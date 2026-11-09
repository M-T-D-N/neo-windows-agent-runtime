"""Read-only fallback to the exact running managed backend when its proxy is down.

This never starts a process, changes ports/configuration, or retries a tool call.
"""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


def _path(value):
    return os.path.normcase(os.path.abspath(value))


def _option(command, name):
    command = command or []
    values = []
    for index, value in enumerate(command):
        if value.startswith(name + '='):
            values.append(value[len(name) + 1:])
        elif value == name and index + 1 < len(command):
            values.append(command[index + 1])
    return values[0] if len(values) == 1 else None


def select_backend(plan, processes, listeners, read_config, digest):
    """Return endpoint and identities only for one exact parent/child/listener chain."""
    endpoint = urlsplit(plan['mcp_endpoint'])
    override = plan.get('server_override') or {}
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
            or endpoint.path != '/mcp' or not override.get('enabled')):
        return None
    browser_exe = _path(Path(plan['proposed_runtime_root']) / 'Chrome-bin/chrome.exe')
    profile = _path(plan['profile_root'])
    browsers = [p for p in processes if p.get('exe') and _path(p['exe']) == browser_exe
                and _option(p['cmdline'], '--user-data-dir')
                and _path(_option(p['cmdline'], '--user-data-dir')) == profile
                and not any(arg.startswith('--type=') for arg in p['cmdline'])]
    if len(browsers) != 1:
        return None
    browser = browsers[0]
    if any(p.get('exe') and _path(p['exe']) == browser_exe and not p.get('cmdline')
           and p['ppid'] != browser['pid'] for p in processes):
        return None  # An unreadable possible second browser root is ambiguous.
    servers = [p for p in processes if p['ppid'] == browser['pid']
               and p.get('exe') and _path(p['exe']) == _path(override['executable'])
               and p['create_time'] >= browser['create_time']]
    if len(servers) != 1:
        return None
    server = servers[0]
    config = _option(server['cmdline'], '--config')
    if not config or _path(config) != _path(Path(profile) / '.browseros/config.json'):
        return None
    ports = read_config(config).get('ports', {})
    port = ports.get('server')
    if (type(port) is not int or not 1 <= port <= 65535 or port == endpoint.port
            or str(port) != _option(browser['cmdline'], '--browseros-server-port')
            or str(endpoint.port) != _option(browser['cmdline'], '--browseros-proxy-port')):
        return None
    matches = [item for item in listeners if item['port'] == port]
    if matches != [{'address': '127.0.0.1', 'port': port, 'pid': server['pid']}]:
        return None
    if digest(server['exe']) != override['sha256']:
        return None
    return {'endpoint': f'http://127.0.0.1:{port}/mcp', 'processes': [browser, server]}


def verified_backend_endpoint(plan):
    import psutil
    try:
        if not (plan.get('server_override') or {}).get('enabled'):
            return None
        rows = []
        for proc in psutil.process_iter(['pid', 'name']):
            if (proc.info['name'] or '').lower() not in ('chrome.exe', 'browseros-claw-server.exe'):
                continue
            try:
                rows.append(proc.as_dict(attrs=['pid', 'ppid', 'exe', 'cmdline', 'create_time']))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        listeners = [{'address': c.laddr.ip, 'port': c.laddr.port, 'pid': c.pid}
                     for c in psutil.net_connections(kind='tcp') if c.status == psutil.CONN_LISTEN]
        def digest(path):
            with open(path, 'rb') as stream:
                return hashlib.file_digest(stream, 'sha256').hexdigest()
        found = select_backend(plan, rows, listeners,
                               lambda path: json.loads(Path(path).read_text(encoding='utf-8')), digest)
        if found is None:
            return None
        for prior in found['processes']:
            if psutil.Process(prior['pid']).as_dict(attrs=list(prior)) != prior:
                return None
        return found['endpoint']
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


if __name__ == '__main__':
    import sys
    print(json.dumps({'endpoint': verified_backend_endpoint(json.load(sys.stdin))}))
