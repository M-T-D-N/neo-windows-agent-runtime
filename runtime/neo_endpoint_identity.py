"""Read-only endpoint guard, plus explicit serialized launcher record repair."""
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
import ntpath
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from neo_transport import (
    BrowserRecordStale, OwnedEndpointTransition, absolute_path, browser_has_type, browser_profile,
)


def path(value):
    return ntpath.normcase(ntpath.abspath(value))


def created(process, recorded):
    return abs(process['create_time']-datetime.fromisoformat(recorded).timestamp()) < .001


def validate(plan, record, server, browser, listeners, config, sha256):
    port=urlsplit(plan['mcp_endpoint']).port
    executable=plan['server_override']['executable']
    expected_config=str(Path(plan['server_state_root'])/'recovery-server-config.json')
    if (server['pid'] != record['server']['ProcessId'] or
            server['ppid'] != record['server']['ParentProcessId'] or
            not created(server,record['server']['CreationDateUtc']) or
            path(server['exe']) != path(executable) or
            len(server['cmdline']) != 2 or path(server['cmdline'][0]) != path(executable) or
            not server['cmdline'][1].startswith('--config=') or
            not absolute_path(server['cmdline'][1].split('=',1)[1]) or
            path(server['cmdline'][1].split('=',1)[1]) != path(expected_config)):
        raise ValueError('MCP listener does not match this installation server')
    if (listeners != [('127.0.0.1',port,server['pid'])] or
            config['ports']['server'] != port or config['ports']['cdp'] != plan['cdp_port'] or
            path(config['directories']['resources']) != path(plan['server_override']['resources_root']) or
            sha256 != plan['server_override']['sha256']):
        raise ValueError('MCP address/config/binary identity mismatch')
    if browser is None:
        # The exact owned server can briefly outlive a normal browser quit.
        # It is not ready and receives no MCP traffic. Only an explicit browser
        # request may enter the launcher's existing verified orphan recovery.
        return False
    validate_browser(plan,record,browser)
    return True


def validate_browser(plan,record,browser):
    profile=browser_profile(browser['cmdline'])
    if (browser['pid'] != record['browser']['ProcessId'] or
            not created(browser,record['browser']['CreationDateUtc']) or
            path(browser['exe']) != path(Path(plan['proposed_runtime_root'])/'Chrome-bin/chrome.exe') or
            '--disable-browseros-server' not in browser['cmdline'] or
            profile is None or path(profile) != path(plan['profile_root']) or
            browser_has_type(browser['cmdline'])):
        raise ValueError('MCP browser/profile identity mismatch')


def session_id(pid):
    """Windows desktop session, independent of executable/profile spelling."""
    import ctypes
    from ctypes import wintypes
    value=wintypes.DWORD()
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes=[wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype=wintypes.BOOL
    if not kernel.ProcessIdToSessionId(pid,ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.value


def rebind_browser(plan):
    """Explicit launcher repair only, while its installation mutex is held.

    Readiness/endpoint guards never write or acquire a new browser. An unchanged
    owned server may reconnect to the new browser on the same dedicated CDP port.
    A live/reused prior PID, ambiguity or a changing snapshot preserves the record.
    """
    from neo_transport import _option
    from workspace_storage import atomic_write_json, ensure_no_reparse_components
    if plan.get('runtime_mode') != 'DIRECT_LOOPBACK':
        raise ValueError('Browser record repair requires the direct installation')
    state=Path(plan['server_state_root'])
    record_path=state/'recovery-server-process.json'
    config_path=state/'recovery-server-config.json'
    ensure_no_reparse_components(record_path)
    ensure_no_reparse_components(config_path)
    raw=record_path.read_bytes()
    config_raw=config_path.read_bytes()
    record=json.loads(raw)
    config=json.loads(config_raw)
    attrs=['pid','ppid','exe','cmdline','create_time']
    old_pid=record['browser']['ProcessId']
    if psutil.pid_exists(old_pid):
        # A valid recorded browser needs no repair, including while its server
        # has not bound a listener yet. Readiness retains its original wait.
        validate_browser(plan,record,psutil.Process(old_pid).as_dict(attrs=attrs))
        return False
    server_process=psutil.Process(record['server']['ProcessId'])
    server=server_process.as_dict(attrs=attrs)
    ports={urlsplit(plan['mcp_endpoint']).port,plan['cdp_port']}
    def listeners():
        return sorted((c.laddr.ip,c.laddr.port,c.pid) for c in psutil.net_connections(kind='tcp')
                      if c.status == psutil.CONN_LISTEN and c.laddr.port in ports)
    observed=listeners()
    mcp=[c for c in observed if c[1] == urlsplit(plan['mcp_endpoint']).port]
    with Path(plan['server_override']['executable']).open('rb') as stream:
        sha=hashlib.file_digest(stream,'sha256').hexdigest()
    validate(plan,record,server,None,mcp,config,sha)  # Verify before allowing any write.
    candidates=[]
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() != 'chrome.exe':
            continue
        try:
            row=process.as_dict(attrs=attrs)
        except psutil.NoSuchProcess:
            continue
        # AccessDenied is deliberately not ignored: a possible second root is unknown.
        if (row['exe'] and path(row['exe']) == path(Path(plan['proposed_runtime_root'])/'Chrome-bin/chrome.exe')
                and browser_profile(row['cmdline'])
                and path(browser_profile(row['cmdline'])) == path(plan['profile_root'])
                and not browser_has_type(row['cmdline'])):
            candidates.append(row)
    if len(candidates) != 1:
        raise ValueError('Replacement browser is absent or ambiguous; preserve identity record')
    browser=candidates[0]
    current=psutil.Process()
    browser_process=psutil.Process(browser['pid'])
    if (server_process.username() != current.username() or browser_process.username() != current.username()
            or session_id(server['pid']) != session_id(current.pid)
            or session_id(browser['pid']) != session_id(current.pid)):
        raise ValueError('Replacement browser/server user or desktop session mismatch')
    if (browser['create_time'] <= datetime.fromisoformat(record['browser']['CreationDateUtc']).timestamp()
            or _option(browser['cmdline'],'--browseros-cdp-port') != str(plan['cdp_port'])
            or [c for c in observed if c[1] == plan['cdp_port']] != [('127.0.0.1',plan['cdp_port'],browser['pid'])]):
        raise ValueError('Replacement browser does not own the dedicated CDP endpoint')
    replacement=copy.deepcopy(record)
    replacement['browser']={'ProcessId':browser['pid'],
        'CreationDateUtc':datetime.fromtimestamp(browser['create_time'],timezone.utc).isoformat()}
    validate(plan,replacement,server,browser,mcp,config,sha)
    if (server_process.as_dict(attrs=attrs) != server or browser_process.as_dict(attrs=attrs) != browser
            or listeners() != observed or psutil.pid_exists(old_pid)
            or record_path.read_bytes() != raw or config_path.read_bytes() != config_raw):
        raise ValueError('Runtime identity changed during repair; preserve identity record')
    atomic_write_json(record_path,replacement,backup_count=0)
    return True


def inspect_owned_endpoint(plan, endpoint=None):
    if plan.get('runtime_mode') != 'DIRECT_LOOPBACK':
        raise ValueError('Unsupported portable runtime mode')
    if endpoint is not None and endpoint != plan['mcp_endpoint']:
        raise ValueError('Alternate endpoint is not qualified for this direct installation')
    port=urlsplit(plan['mcp_endpoint']).port
    cdp_port=plan['cdp_port']
    sockets=[(c.laddr.ip,c.laddr.port,c.pid) for c in psutil.net_connections(kind='tcp')
             if c.status == psutil.CONN_LISTEN and c.laddr.port in (port,cdp_port)]
    listeners=[c for c in sockets if c[1] == port]
    if not listeners:
        return {'verified':False,'code':'MCP_NOT_LISTENING','reason':'MCP endpoint is not listening'}
    state=Path(plan['server_state_root'])
    try:
        record=json.loads((state/'recovery-server-process.json').read_text(encoding='utf-8'))
        server=psutil.Process(record['server']['ProcessId']).as_dict(attrs=['pid','ppid','exe','cmdline','create_time'])
        try:
            browser=psutil.Process(record['browser']['ProcessId']).as_dict(attrs=['pid','exe','cmdline','create_time'])
        except psutil.NoSuchProcess:
            browser=None
        config=json.loads((state/'recovery-server-config.json').read_text(encoding='utf-8'))
        with Path(plan['server_override']['executable']).open('rb') as stream:
            sha=hashlib.file_digest(stream,'sha256').hexdigest()
        ready=validate(plan,record,server,browser,listeners,config,sha)
        if psutil.Process(server['pid']).as_dict(attrs=list(server)) != server:
            raise ValueError('Server identity changed during inspection')
        cdp=[c for c in sockets if c[1] == cdp_port]
        if ready:
            if not cdp:
                return {'verified':False,'code':'CDP_NOT_LISTENING',
                        'reason':'MCP is listening; the recorded browser does not yet have its CDP listener',
                        'browserPid':browser['pid'],'mcpPid':server['pid']}
            if cdp != [('127.0.0.1',cdp_port,browser['pid'])]:
                raise ValueError('Recorded browser and CDP listener do not match; preserve both processes')
            return {'verified':True,'code':'READY','reason':'',
                    'browserPid':browser['pid'],'browserCreated':browser['create_time'],
                    'mcpPid':server['pid']}
        previous=record['browser']['ProcessId']
        if cdp and (len(cdp) != 1 or cdp[0][0] != '127.0.0.1' or not cdp[0][2] or cdp[0][2] == previous):
            raise ValueError('CDP listener changed or is ambiguous; preserve the existing processes')
        return {'verified':False,
                'code':'BROWSER_RECORD_STALE' if cdp else 'RECORDED_BROWSER_EXITED',
                'reason':('MCP is listening; recorded browser exited and a replacement CDP owner needs verification'
                          if cdp else 'MCP is listening; recorded browser exited and CDP is not listening'),
                'recordedBrowserPid':previous,'mcpPid':server['pid'],
                'cdpPid':cdp[0][2] if cdp else None}
    except psutil.NoSuchProcess as error:
        # The listener snapshot may race normal server exit. Absence is not
        # permission to contact a replacement listener, so inspect again.
        if not any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                   for c in psutil.net_connections(kind='tcp')):
            return {'verified':False,'code':'MCP_NOT_LISTENING','reason':'MCP endpoint is not listening'}
        raise ValueError('Endpoint process changed; preserve existing listener') from error
    except (OSError,KeyError,TypeError,psutil.Error) as error:
        raise ValueError('Endpoint ownership is unverified; preserve the existing listener') from error


def assert_owned_endpoint(plan, endpoint=None):
    return inspect_owned_endpoint(plan,endpoint)['verified']


def transport_browser_identity(plan, endpoint):
    state=inspect_owned_endpoint(plan,endpoint)
    if state['code'] == 'BROWSER_RECORD_STALE':
        raise BrowserRecordStale(state['reason'])
    if state['code'] in ('RECORDED_BROWSER_EXITED', 'CDP_NOT_LISTENING'):
        raise OwnedEndpointTransition(state['reason'])
    if not state['verified']:
        return False
    return state['browserPid'],state['browserCreated']


if __name__ == '__main__':
    import argparse
    import sys
    parser=argparse.ArgumentParser()
    parser.add_argument('--endpoint',required=True)
    parser.add_argument('--rebind-browser',action='store_true')
    args=parser.parse_args()
    try:
        plan=json.load(sys.stdin)
        if args.endpoint != plan['mcp_endpoint']:
            raise ValueError('Alternate endpoint is not qualified for this direct installation')
        if args.rebind_browser:
            print(json.dumps({'rebound':rebind_browser(plan),'reason':''}))
            raise SystemExit(0)
        print(json.dumps(inspect_owned_endpoint(plan,args.endpoint)))
    except Exception as error:
        print(json.dumps({'verified':False,'code':'IDENTITY_UNVERIFIED','reason':f'{type(error).__name__}: {error}'}))
        raise SystemExit(2)
