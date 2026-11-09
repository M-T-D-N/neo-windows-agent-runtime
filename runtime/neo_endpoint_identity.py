"""Read-only identity check before the portable bridge contacts a live endpoint."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import psutil


def path(value):
    return os.path.normcase(os.path.abspath(value))


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
            server['cmdline'][1] != '--config='+expected_config):
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
    if (browser['pid'] != record['browser']['ProcessId'] or
            not created(browser,record['browser']['CreationDateUtc']) or
            path(browser['exe']) != path(Path(plan['proposed_runtime_root'])/'Chrome-bin/chrome.exe') or
            '--disable-browseros-server' not in browser['cmdline'] or
            not any(arg.startswith('--user-data-dir=') and path(arg.split('=',1)[1]) == path(plan['profile_root']) for arg in browser['cmdline']) or
            any(arg.startswith('--type=') for arg in browser['cmdline'])):
        raise ValueError('MCP browser/profile identity mismatch')
    return True


def assert_owned_endpoint(plan, endpoint=None):
    if plan.get('runtime_mode') != 'DIRECT_LOOPBACK':
        raise ValueError('Unsupported portable runtime mode')
    if endpoint is not None and endpoint != plan['mcp_endpoint']:
        raise ValueError('Alternate endpoint is not qualified for this direct installation')
    port=urlsplit(plan['mcp_endpoint']).port
    listeners=[(c.laddr.ip,c.laddr.port,c.pid) for c in psutil.net_connections(kind='tcp')
               if c.status == psutil.CONN_LISTEN and c.laddr.port == port]
    if not listeners:
        return False  # No listener; no readiness claim and no browser startup here.
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
        return ready
    except psutil.NoSuchProcess as error:
        # The listener snapshot may race normal server exit. Absence is not
        # permission to contact a replacement listener, so inspect again.
        if not any(c.status == psutil.CONN_LISTEN and c.laddr.port == port
                   for c in psutil.net_connections(kind='tcp')):
            return False
        raise ValueError('Endpoint process changed; preserve existing listener') from error
    except (OSError,KeyError,TypeError,psutil.Error) as error:
        raise ValueError('Endpoint ownership is unverified; preserve the existing listener') from error


if __name__ == '__main__':
    import argparse
    import sys
    parser=argparse.ArgumentParser()
    parser.add_argument('--endpoint',required=True)
    args=parser.parse_args()
    try:
        verified=assert_owned_endpoint(json.load(sys.stdin),args.endpoint)
        print(json.dumps({'verified':verified,'reason':'' if verified else 'Endpoint is not listening'}))
    except Exception as error:
        print(json.dumps({'verified':False,'reason':f'{type(error).__name__}: {error}'}))
        raise SystemExit(2)
