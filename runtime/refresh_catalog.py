"""Read official schemas from an already running verified Neo, never launch it."""
import argparse
import hashlib
import json
from pathlib import Path
import httpx
from workspace_storage import atomic_write_json
from neo_endpoint_identity import assert_owned_endpoint

def result(response, rpc_id):
    response.raise_for_status()
    if 'text/event-stream' in response.headers.get('content-type',''):
        messages = [json.loads(line[5:].strip()) for line in response.text.splitlines()
                    if line.startswith('data:') and line[5:].strip()]
    else:
        messages = [response.json()]
    for message in messages:
        if message.get('id') == rpc_id and 'result' in message:
            return message['result']
    raise ValueError('Official schema response missing')

def refresh(root):
    root = Path(root)
    plan = json.loads((root/'runtime-plan.json').read_text(encoding='utf-8'))
    override = plan['server_override']
    digest = hashlib.sha256(Path(override['executable']).read_bytes()).hexdigest()
    if digest != override['sha256']:
        raise ValueError('Server payload differs from approved plan')
    if not assert_owned_endpoint(plan,plan['mcp_endpoint']):
        raise ValueError('No owned endpoint; preserve existing catalog')
    with httpx.Client(timeout=10,trust_env=False,follow_redirects=False) as client:
        reply = client.post(plan['mcp_endpoint'],json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'workspace-schema-reader','version':'1'}}},
            headers={'Accept':'application/json, text/event-stream'})
        init=result(reply,1)
        if init['serverInfo']['version'] != override['version']:
            raise ValueError('Endpoint server version differs from approved plan')
        headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':init['protocolVersion']}
        if reply.headers.get('Mcp-Session-Id'):
            headers['Mcp-Session-Id']=reply.headers['Mcp-Session-Id']
        if not assert_owned_endpoint(plan,plan['mcp_endpoint']):
            raise ValueError('Endpoint changed; preserve existing catalog')
        tools=result(client.post(plan['mcp_endpoint'],json={'jsonrpc':'2.0','id':2,'method':'tools/list'},headers=headers),2)
    atomic_write_json(root/'official-tools.json',{'schema_version':1,'server_sha256':digest,'initialize':init,'catalog':tools},backup_count=0)
    return len(tools['tools'])

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).parent)
    print(json.dumps({'tools':refresh(parser.parse_args().root),'browser_started':False}))
