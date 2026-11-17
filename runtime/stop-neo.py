"""Quit the verified dedicated Neo after its server has durably drained sessions."""
import argparse
import base64
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
import psutil

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from neo_operation_boundary import OperationBoundary
from neo_transport import verified_backend_context
from process_environment import build_environment

PROCESS_FIELDS = ('pid', 'ppid', 'exe', 'cmdline', 'create_time')
CLOSE_BROWSER = r"""
$ErrorActionPreference='Stop'
$socket=[Net.WebSockets.ClientWebSocket]::new()
$socket.Options.Proxy=$null
$timeout=[Threading.CancellationTokenSource]::new(5000)
try {
    $socket.ConnectAsync([Uri]$env:NEO_OWNED_QUIT_WEBSOCKET,$timeout.Token).GetAwaiter().GetResult()
    $message=[Text.Encoding]::UTF8.GetBytes('{"id":1,"method":"Browser.close","params":{}}')
    $socket.SendAsync([ArraySegment[byte]]::new($message),[Net.WebSockets.WebSocketMessageType]::Text,$true,$timeout.Token).GetAwaiter().GetResult()
    [void]$socket.ReceiveAsync([ArraySegment[byte]]::new((New-Object byte[] 1024)),$timeout.Token).GetAwaiter().GetResult()
} finally {
    $socket.Dispose()
    $timeout.Dispose()
}
"""


def process(row):
    instance = psutil.Process(row['pid'])
    if instance.as_dict(attrs=list(row)) != row:
        raise RuntimeError('Dedicated runtime process identity changed')
    return instance


@contextmanager
def held_process_exit(row):
    """Keep the exact Windows process object alive until its exit code is read."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    def wait(timeout):
        status = kernel.WaitForSingleObject(handle, timeout * 1000)
        if status == 0x102:  # WAIT_TIMEOUT
            raise TimeoutError(f'Process {row["pid"]} did not exit in {timeout} seconds')
        if status != 0:  # WAIT_OBJECT_0
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    process(row)
    # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, row['pid'])
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # Recheck after opening to reject a PID recycled between observation and open.
        process(row)
        yield wait
    finally:
        kernel.CloseHandle(handle)


def ownership(plan):
    if plan.get('runtime_mode') != 'DIRECT_LOOPBACK':
        context = verified_backend_context(plan)
        if not context:
            raise RuntimeError('Exact managed browser, server and loopback endpoint are unverified')
        return context['endpoint'], *context['processes']
    # Public installs already have a verified direct-server identity contract.
    from neo_endpoint_identity import assert_owned_endpoint

    record_path = Path(plan['server_state_root']) / 'recovery-server-process.json'
    raw = record_path.read_bytes()
    record = json.loads(raw)
    browser, server = (
        psutil.Process(record[k]['ProcessId']).as_dict(attrs=list(PROCESS_FIELDS))
        for k in ('browser', 'server')
    )
    if not assert_owned_endpoint(plan) or record_path.read_bytes() != raw:
        raise RuntimeError('Dedicated direct server identity changed')
    process(browser)
    process(server)
    return plan['mcp_endpoint'], browser, server


def database(plan):
    path = Path(plan['server_state_root']) / 'browserclaw.sqlite'
    return closing(sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro', uri=True))


def open_sessions(db):
    return db.execute('''
        SELECT session_id FROM agent_session_starts AS start
        WHERE NOT EXISTS (SELECT 1 FROM agent_session_ends WHERE session_id = start.session_id)
    ''').fetchall()


def verify_drained(db, sessions):
    with db:
        db.execute("BEGIN")
        if open_sessions(db):
            raise RuntimeError('A browser session is still open after shutdown')
        for (session,) in sessions:
            end = db.execute(
                'SELECT kind, reason, created_at FROM agent_session_ends WHERE session_id = ?',
                (session,),
            ).fetchall()
            if len(end) != 1 or end[0][0] != 'closed' or end[0][1] not in ('server shutdown', 'idle timeout'):
                raise RuntimeError('Normal session closure did not persist; keeping the browser open')
            if db.execute('''
                SELECT 1 FROM session_tabs WHERE session_id = ?
                AND (released_at IS NULL OR released_at > ? OR released_at < claimed_at)
            ''', (session, end[0][2])).fetchone():
                raise RuntimeError('Session tab claims did not drain; keeping the browser open')


def browser_socket(http, address, browser):
    process(browser)
    port = urlsplit(address).port
    listeners = [(c.laddr.ip, c.pid) for c in psutil.net_connections(kind='tcp')
                 if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
    if listeners != [('127.0.0.1', browser['pid'])]:
        raise RuntimeError('CDP endpoint is not owned by the exact dedicated browser')
    response = http.get(address + '/json/version')
    response.raise_for_status()
    ws = response.json().get('webSocketDebuggerUrl', '')
    if not re.fullmatch(r'ws://127\.0\.0\.1:' + str(port) + r'/devtools/browser/[0-9a-fA-F-]{36}', ws):
        raise RuntimeError('Unexpected dedicated browser websocket identity')
    process(browser)
    return ws


def stop_preserving_tabs(plan):
    with OperationBoundary(Path(plan['ledger_root']) / 'browser-operations.lock', exclusive=True, timeout=8):
        return _stop_under_boundary(plan)


def _stop_under_boundary(plan):
    endpoint, browser, server = ownership(plan)
    backend = endpoint.removesuffix('/mcp')
    cdp_port = plan.get('cdp_port', 9111)
    cdp = f'http://127.0.0.1:{cdp_port}'
    with (
        held_process_exit(server) as wait_server,
        held_process_exit(browser) as wait_browser,
        httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as http,
    ):
        socket = browser_socket(http, cdp, browser)
        with database(plan) as db:
            active = open_sessions(db)
        process(server)
        response = http.post(backend + '/system/shutdown', json={})
        response.raise_for_status()
        if response.json().get('status') != 'ok':
            raise RuntimeError('Server did not acknowledge shutdown')
        try:
            server_exit = wait_server(15)
        except TimeoutError as error:
            raise RuntimeError('Server did not finish draining; keeping the browser open') from error
        if server_exit != 0:
            raise RuntimeError(f'Server exited with code {server_exit}; keeping the browser open')
        if browser_socket(http, cdp, browser) != socket:
            raise RuntimeError('Browser connection changed after server drain')
        with database(plan) as db:
            verify_drained(db, active)
        # Chromium can replace its exited sidecar. That must not authorize any
        # new work during this maintenance boundary. A live local session above
        # blocks closure, and supported MCP clients wait at the profile-wide lock.
        env = build_environment(os.environ, {'NEO_OWNED_QUIT_WEBSOCKET': socket},
                                remove=('PSModulePath',))
        command = base64.b64encode(CLOSE_BROWSER.encode('utf-16le')).decode('ascii')
        close = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
                               env=env, cwd=plan['server_state_root'], timeout=10,
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        # The socket may disappear as soon as the browser handles Browser.close.
        # Only the original process's exit proves that the requested quit occurred.
        try:
            browser_exit = wait_browser(10)
        except TimeoutError as error:
            raise RuntimeError('Dedicated browser quit was not verified; inspect before retrying') from error
        if browser_exit != 0:
            raise RuntimeError(f'Dedicated browser exited with code {browser_exit}')
        ports = {cdp_port, urlsplit(endpoint).port, urlsplit(plan['mcp_endpoint']).port}
        deadline = time.monotonic() + 5
        while any(c.status == psutil.CONN_LISTEN and c.laddr.port in ports
                  for c in psutil.net_connections(kind='tcp')):
            if time.monotonic() >= deadline:
                raise RuntimeError('Dedicated runtime port still has a listener after quit')
            time.sleep(0.1)
        return {'browserClosed': True, 'drainedSessions': len(active),
                'browserPid': browser['pid'], 'serverPid': server['pid'],
                'browserExitCode': browser_exit, 'serverExitCode': server_exit,
                'dispatchStatus': close.returncode}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirmed-safe', action='store_true', required=True,
                        help='The operator verified shutdown scope, no ongoing requests, and no unsaved input.')
    parser.parse_args()
    plan = json.loads((ROOT / 'runtime-plan.json').read_text(encoding='utf-8'))
    print(json.dumps(stop_preserving_tabs(plan)))
