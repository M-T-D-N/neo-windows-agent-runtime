"""Read-only fallback to the exact running managed backend when its proxy is down.

This never starts a process, changes ports/configuration, or retries a tool call.
"""
import hashlib
import json
import os
import ntpath
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


def _path(value):
    return ntpath.normcase(ntpath.abspath(value))


def absolute_path(value):
    # On Windows these predicates coincide. Native absolute fixture paths also
    # permit read-only pure-function review on a non-Windows host.
    return ntpath.isabs(value) or os.path.isabs(value)


def _browser_arguments(command):
    # This is an ownership qualification contract, not a general Chromium CLI
    # emulator. Our launcher never emits normalization-dependent argument edges,
    # switch terminators or the raw-string single-argument mode. Preserve them
    # as unknown rather than interpreting a profile differently from Chromium.
    if not command or any(not isinstance(arg,str) or not arg or any(
            ch.isspace() or unicodedata.category(ch)[0] in 'CZ' for ch in (arg[0],arg[-1])) for arg in command):
        return None
    arguments=command[1:]  # argv[0] is the program, never a profile switch.
    if any(arg == '--' or re.match(r'^(?:--|-|/)single-argument(?:=|$)',arg,re.I) for arg in arguments):
        return None
    return arguments


def browser_profile(command):
    """Reject duplicate/alias Chromium profile switches, including on Windows.

    The dedicated launcher emits exactly one canonical --user-data-dir=value.
    Count other Chromium prefixes and casing too, rather than silently ignoring
    a later alias that can change the effective profile. Inputs are OS argv.
    """
    arguments=_browser_arguments(command)
    if arguments is None:
        return None
    values=[arg for arg in arguments if re.match(r'^(?:--|-|/)user-data-dir(?:=|$)',arg,re.I)]
    if len(values) != 1 or not values[0].startswith('--user-data-dir='):
        return None
    value=values[0].split('=',1)[1]
    return value if absolute_path(value) else None


def browser_has_type(command):
    arguments=_browser_arguments(command)
    return arguments is None or any(re.match(r'^(?:--|-|/)type(?:=|$)',arg,re.I) for arg in arguments)


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
                and browser_profile(p['cmdline'])
                and _path(browser_profile(p['cmdline'])) == profile
                and not browser_has_type(p['cmdline'])]
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


def verified_backend_context(plan):
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
        return found
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        return None


def verified_backend_endpoint(plan):
    context = verified_backend_context(plan)
    return context['endpoint'] if context else None


if __name__ == '__main__':
    import sys
    print(json.dumps({'endpoint': verified_backend_endpoint(json.load(sys.stdin))}))
