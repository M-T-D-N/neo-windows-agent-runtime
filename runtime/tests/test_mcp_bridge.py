import asyncio
import json
import sys
import subprocess
import os
from pathlib import Path
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge, SESSION_META_KEY, TOOL_PROTOCOL, UpstreamError
import neo_mcp_bridge

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "codex-test", "version": "1"}}}
CALL = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "run", "arguments": {"code": "return 1"}}}

class StdioEncodingTests(unittest.TestCase):
    def test_real_initialize_uses_utf8_even_with_windows_legacy_encoding(self):
        environment=dict(os.environ,PYTHONUTF8='0',PYTHONIOENCODING='cp949')
        result=subprocess.run([sys.executable,'-B',str(Path(neo_mcp_bridge.__file__)),'--client','agy'],
            input=(json.dumps(INIT)+'\n').encode('utf-8'),capture_output=True,env=environment,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8'))
        reply=json.loads(result.stdout.decode('utf-8'))
        self.assertNotIn('error',reply)
        self.assertEqual(reply['result']['serverInfo']['name'],'browseros-neo')


def response(request, result=None, status=200, session="first"):
    message = json.loads(request.content)
    value = ((result or {}).get('structuredContent') or {}).get('value')
    if (isinstance(value, dict) and 'pages' in value and 'targets' not in value
            and 'Target.getTargets' in (message.get('params',{}).get('arguments',{}).get('code') or '')):
        value['targets'] = [p['targetId'] for p in value['pages'] if p.get('targetId')]
    return httpx.Response(status, headers={"Mcp-Session-Id": session}, json={
        "jsonrpc": "2.0", "id": message.get("id"), "result": result or {}})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_parent_with_used_user_child_reconciles_without_claiming(self):
        import tempfile
        from unittest.mock import AsyncMock
        task='00000000-0000-4000-8000-000000000001'
        parent=dict(pageId=1,tabId=101,targetId='parent',groupId='g',url='https://example.com/parent',ownership='mine')
        child=dict(pageId=2,tabId=202,targetId='child',groupId='g',url='https://example.com/child',ownership='user',ownerAgentId=None)
        for prior_closed, same_url in ((False, False), (True, False), (False, True), (True, True)):
            child['url'] = parent['url'] if same_url else 'https://example.com/child'
            with self.subTest(prior_closed=prior_closed, same_url=same_url), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                pages=[child] if prior_closed else [parent,child]
                groups=[dict(groupId='g',title='session',tabIds=[p['tabId'] for p in pages])]
                actions=[]
                def handler(request):
                    rpc=json.loads(request.content);name=rpc['params']['name'];args=rpc['params']['arguments']
                    if name=='tabs':
                        actions.append(args['action']);self.assertEqual(args['action'],'close');self.assertEqual(args['page'],1)
                        pages.remove(parent);groups[0]['tabIds']=[202]
                        return response(request,dict(content=[dict(type='text',text='closed page 1')]))
                    code=args['code']
                    value=(dict(pages=list(pages),groups=groups,targets=[p['targetId'] for p in pages])
                           if 'Browser.getTabGroups' in code else
                           dict(pages=list(pages),tabs=list(pages),targets=[p['targetId'] for p in pages])
                           if 'Browser.getTabs' in code else list(pages))
                    return response(request,dict(structuredContent=dict(ok=True,value=value)))
                output=[];bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                bridge.initialize=INIT;bridge.tool_session='original';bridge.ensure_ready=AsyncMock()
                bridge.task_state.save('original',[parent],[dict(groupId='g',title='session',tabIds=[101,202])])
                used=dict(targetId='child',tabId=202,url=child['url'],purpose='Inspect the link-opened child')
                bridge.task_state.record['used_tabs']=[used];bridge.cleanup.used={'child':used}
                try:
                    if prior_closed:
                        recovery=dict(prior_group_id='g',group_id='g',pages=[dict(targetId='child',url=child['url'])],
                                      reason='Human approved cleanup of the already registered child')
                        await bridge.prepare_task(recovery=recovery)
                    else:
                        await bridge.handle(dict(jsonrpc='2.0',id=12,method='tools/call',
                            params=dict(name='tabs',arguments=dict(action='close',page=1))))
                        self.assertNotIn('WORKSPACE_TRACKING_FAILED',json.dumps(output[-1]))
                    self.assertEqual(bridge.task_state.record['pages'],[])
                    await bridge.handle(dict(jsonrpc='2.0',id=13,method='tools/call',
                        params=dict(name='workspace_cleanup',arguments=dict(action='resume',task_id=task))))
                    check=output[-1]['result']['structuredContent']
                    self.assertFalse(check.get('pendingReconciliationGroups'))
                    self.assertEqual(check['pendingUsedTargetIds'],['child'])
                    self.assertEqual(child['ownership'],'user')
                    self.assertEqual(actions,[] if prior_closed else ['close'])
                    self.assertEqual(pages,[child])
                finally:
                    await bridge.close()

    async def test_known_user_sibling_cannot_fall_back_to_url_reclaim(self):
        import tempfile
        from unittest.mock import AsyncMock
        task='00000000-0000-4000-8000-000000000001'
        parent=dict(pageId=1,tabId=101,targetId='parent',groupId='g',url='https://example.com/same',ownership='mine')
        child=dict(pageId=2,tabId=202,targetId='child',groupId='g',url=parent['url'],ownership='user',ownerAgentId=None)
        for case in ('unregistered','url_mismatch','tab_mismatch','current_tab_mismatch','foreign_alias','raw_parent_sdk_missing'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                current=dict(child, tabId=303 if case=='current_tab_mismatch' else 202)
                pages=[current]
                if case=='foreign_alias':pages.append(dict(current,pageId=3,ownership='other-agent',ownerAgentId='foreign'))
                groups=[dict(groupId='g',title='session',tabIds=[current['tabId']])]
                actions=[]
                targets=['parent','child'] if case=='raw_parent_sdk_missing' else ['child']
                def handler(request):
                    rpc=json.loads(request.content);name=rpc['params']['name']
                    if name!='run':actions.append(name);raise AssertionError('Reclaim/create/close must not dispatch')
                    return response(request,dict(structuredContent=dict(ok=True,value=dict(pages=pages,groups=groups,targets=targets))))
                output=[];bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                bridge.initialize=INIT;bridge.tool_session='original';bridge.ensure_ready=AsyncMock()
                bridge.task_state.save('original',[parent],[dict(groupId='g',title='session',tabIds=[101,202])])
                used=dict(targetId='child',tabId=303 if case=='tab_mismatch' else 202,
                          url='https://example.com/changed' if case=='url_mismatch' else child['url'],purpose='Known user sibling')
                bridge.task_state.record['used_tabs']=[] if case=='unregistered' else [used]
                bridge.task_state._write_record()
                path=Path(root)/(task+'.json');before=path.read_bytes()
                try:
                    candidates,conflicts=bridge.task_state.reconciliation(pages,groups,native_targets=targets)
                    self.assertEqual(candidates,[]);self.assertTrue(conflicts)
                    with self.assertRaisesRegex(UpstreamError,'ownership reconciliation'):
                        await bridge.prepare_task()
                    await bridge.handle(dict(jsonrpc='2.0',id=12,method='tools/call',params=dict(name='workspace_cleanup',arguments=dict(action='resume',task_id=task))))
                    self.assertIn('block',json.dumps(output[-1]))
                    self.assertEqual(actions,[])
                    self.assertEqual(path.read_bytes(),before)
                    self.assertEqual(current['ownership'],'user')
                finally:
                    await bridge.close()

    async def test_inventory_disagreement_preserves_saved_record_unless_absence_proven(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        a=dict(pageId=1,targetId='a',groupId='a-group',url='https://example.com/a',ownership='mine')
        b=dict(pageId=2,targetId='b',groupId='b-group',url='https://example.com/b',ownership='mine')
        groups=[dict(groupId=p['groupId'],title='same title') for p in [a,b]]
        for evidence in ({'absent':[],'present':'a'}, {}, {'absent':[]}, {'absent':['different']},
                         {'absent':['a'],'present':'a'}, {'absent':['a']}):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                calls=[]
                def handler(request):
                    code=json.loads(request.content)['params']['arguments']['code'];calls.append(code)
                    value=(dict(pages=[a,b],groups=groups[1:],targets=['b'])
                           if 'Target.getTargets' in code else evidence)
                    return response(request,dict(structuredContent=dict(ok=True,value=value)))
                bridge=Bridge('http://127.0.0.1:9010/mcp',lambda x:None,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                bridge.initialize=INIT;bridge.tool_session='original'
                bridge.task_state.save('original',[a,b],groups)
                path=Path(root)/(task+'.json');before=path.read_bytes()
                try:
                    if evidence == {'absent':['a']}:
                        await bridge.remember_task()
                        self.assertEqual([p['targetId'] for p in json.loads(path.read_text())['pages']],['b'])
                        await bridge.prepare_task()
                        self.assertEqual(bridge.task_state.record['pages'][0]['targetId'],'b')
                    else:
                        with self.assertRaisesRegex(UpstreamError,'preserve prior bindings'):
                            await bridge.remember_task()
                        self.assertEqual(path.read_bytes(),before)
                    self.assertIn('Target.getTargetInfo',calls[1])
                    self.assertIn('No target with given id found',calls[1])
                finally:
                    await bridge.close()

    async def test_malformed_native_target_inventory_preserves_record(self):
        import tempfile
        for targets in (None,'a',[None],[''],[5]):
            with self.subTest(targets=targets), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                page=dict(pageId=1,targetId='a',groupId='g',url='https://example.com/a',ownership='mine')
                def handler(request):
                    return response(request,dict(structuredContent=dict(ok=True,value=dict(pages=[page],groups=[],targets=targets))))
                bridge=Bridge('http://127.0.0.1:9010/mcp',lambda x:None,state_root=root,
                    task_id='00000000-0000-4000-8000-000000000001',client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                bridge.initialize=INIT;bridge.tool_session='original';bridge.task_state.save('original',[page],[])
                path=Path(root)/(bridge.task_state.task_id+'.json');before=path.read_bytes()
                try:
                    with self.assertRaisesRegex(UpstreamError,'preserve prior bindings'):
                        await bridge.remember_task()
                    self.assertEqual(path.read_bytes(),before)
                finally:
                    await bridge.close()

    async def test_close_last_group_member_updates_binding_despite_duplicate_titles(self):
        import tempfile
        from unittest.mock import AsyncMock
        task='00000000-0000-4000-8000-000000000001'
        closed=dict(pageId=1,tabId=101,targetId='closed',groupId='closed-group',
                    url='https://example.com/temporary',ownership='mine')
        keep=dict(pageId=2,tabId=102,targetId='keep',groupId='keep-group',
                  url='https://example.com/follow-up',ownership='mine')
        pages=[closed,keep]
        groups=[dict(groupId=p['groupId'],title='same session title') for p in pages]
        closes=[]
        def handler(request):
            rpc=json.loads(request.content)
            if rpc['params']['name']=='tabs':
                self.assertEqual(rpc['params']['arguments']['action'],'close')
                closes.append(rpc['params']['arguments']['page'])
                pages.remove(closed);groups.pop(0)
                return response(request,dict(content=[dict(type='text',text='closed page 1')]))
            code=rpc['params']['arguments']['code']
            value=(dict(pages=list(pages),groups=list(groups),targets=[p['targetId'] for p in pages])
                   if 'Browser.getTabGroups' in code else list(pages))
            return response(request,dict(structuredContent=dict(ok=True,value=value)))
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            output=[];bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=root,task_id=task,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            self.addAsyncCleanup(bridge.close)
            bridge.initialize=INIT;bridge.tool_session='original-session';bridge.ensure_ready=AsyncMock()
            retention=(('original-session','keep',keep['url']),'Confirmed next task needs this page')
            bridge.cleanup.retained=dict([retention])
            bridge.task_state.save('original-session',pages,groups,[retention])
            async def invoke(name,args):
                await bridge.handle(dict(jsonrpc='2.0',id=12,method='tools/call',params=dict(name=name,arguments=args)))
                return output[-1]['result']
            result=await invoke('tabs',dict(action='close',page=1))
            self.assertFalse(result.get('isError'))
            self.assertNotIn('WORKSPACE_TRACKING_FAILED',json.dumps(result))
            record=json.loads((Path(root)/(task+'.json')).read_text(encoding='utf-8'))
            self.assertEqual([p['targetId'] for p in record['pages']],['keep'])
            self.assertEqual(record['retained'][0][1],retention[1])
            result=await invoke('workspace_cleanup',dict(action='resume',task_id=task))
            self.assertNotEqual(result['structuredContent'].get('decision'),'block')
            self.assertEqual(closes,[1])

    async def test_reclaim_is_internal_and_cannot_dispatch_from_public_tools(self):
        calls = []
        bridge, output = await self.make_bridge(lambda request: calls.append(request))
        bridge.catalog['catalog']['tools'] = [{'name': 'tabs', 'inputSchema': {'properties': {
            'action': {'enum': ['list', 'new', 'reclaim', 'reclaim_expired','reclaim_restored']},
            'expected': {'type': 'array'},'priorSession':{'type':'string'},
            'priorGroupId':{'type':'string'},'prior':{'type':'array'}}}}]
        await bridge.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        properties = output[-1]['result']['tools'][0]['inputSchema']['properties']
        self.assertEqual(properties['action']['enum'], ['list', 'new'])
        self.assertNotIn('expected', properties)
        self.assertNotIn('priorSession',properties)
        self.assertNotIn('priorGroupId',properties)
        self.assertNotIn('prior',properties)
        self.assertIn('reclaim_expired', bridge.catalog['catalog']['tools'][0]['inputSchema']['properties']['action']['enum'])
        for args in ({'action': 'reclaim', 'groupId': 'foreign'}, {'action': 'new', 'expected': []},
                     {'action':'reclaim_expired','groupId':'foreign'}, {'action':'new','priorSession':'foreign'},
                     {'action':'reclaim_restored','groupId':'foreign'}, {'action':'new','priorGroupId':'foreign'},
                     {'action':'new','prior':[]}):
            await bridge.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                 'params': {'name': 'tabs', 'arguments': args}})
            self.assertTrue(output[-1]['result']['isError'])
            self.assertIn('internal', output[-1]['result']['content'][0]['text'])
        self.assertEqual(calls, [])

    async def make_bridge(self, handler, starter=None, deadline=1):
        output = []
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        bridge = Bridge("http://127.0.0.1:9010/mcp", output.append,
                        client=client, starter=starter, startup_seconds=deadline)
        self.addAsyncCleanup(bridge.close)
        return bridge, output

    async def test_readonly_inventory_survives_reconciliation_conflict_without_binding_changes(self):
        import tempfile
        task_id='00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            calls=[]
            pages=[dict(pageId=10,tabId=20,targetId='restored',groupId='old-group',
                        url='https://example.com/login',ownership='user',ownerAgentId=None)]
            groups=[dict(groupId='old-group',title='recorded task')]
            def handler(request):
                rpc=json.loads(request.content);calls.append(rpc)
                if rpc['method']=='ping': return response(request)
                if rpc['params']['name']=='run':
                    return response(request,{'structuredContent':{'ok':True,'value':dict(pages=pages,groups=groups)}})
                return response(request,{'content':[{'type':'text','text':'listed'}]})
            bridge=Bridge('http://127.0.0.1:9010/mcp',lambda m:output.append(m),
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),state_root=root,task_id=task_id)
            self.addAsyncCleanup(bridge.close)
            output=[];bridge.initialize=INIT;bridge.tool_session='prior-session'
            bridge.task_state.save('prior-session',[
                dict(targetId='old-target',groupId='old-group',url='https://example.com/offer',ownership='mine')],
                [dict(groupId='old-group',title='recorded task')],
                retained=[(('prior-session','old-target','https://example.com/offer'),'Preserve approved follow-up')])
            bridge.execution_id='inspection-execution'
            bridge.cleanup_empty_session='prior-session'
            saved=Path(root)/(task_id+'.json');before=json.loads(saved.read_text(encoding='utf-8'))
            for name in ('tabs','windows','tab_groups'):
                with self.subTest(name=name):
                    await bridge.handle({'jsonrpc':'2.0','id':20,'method':'tools/call',
                        'params':{'name':name,'arguments':{'action':'list'}}})
                    self.assertFalse(output[-1]['result'].get('isError',False))
                    after=json.loads(saved.read_text(encoding='utf-8'))
                    for key in ('pages','groups','retained','review_required','session'):
                        self.assertEqual(after.get(key),before.get(key))
                    self.assertTrue(after['execution_incomplete'])
                    self.assertIsNone(bridge.cleanup_empty_session)
                    self.assertFalse(bridge.task_prepared)
            for name in ('snapshot','read','grep','diff','screenshot'):
                with self.subTest(observation=name):
                    await bridge.handle({'jsonrpc':'2.0','id':22,'method':'tools/call',
                        'params':{'name':name,'arguments':{'page':10}}})
                    self.assertFalse(output[-1]['result'].get('isError',False))
                    after=json.loads(saved.read_text(encoding='utf-8'))
                    for key in ('pages','groups','retained','session'):
                        self.assertEqual(after.get(key),before.get(key))
                    self.assertFalse(bridge.task_prepared)
            for name,args in (('tabs',{'action':'new'}),('tabs',{'action':'close','page':10}),
                              ('windows',{'action':'close','windowId':1}),
                              ('tabs',{'action':'list','url':'https://example.com'}),
                              ('run',{'code':'return 1'}),
                              ('evaluate',{'page':10,'code':'document.body.textContent="changed"'}),
                              ('act',{'page':10,'kind':'fill','ref':'e1','value':'changed'})):
                with self.subTest(blocked=name,args=args):
                    await bridge.handle({'jsonrpc':'2.0','id':21,'method':'tools/call',
                        'params':{'name':name,'arguments':args}})
                    self.assertIn('NEO_NOT_READY',output[-1]['result']['content'][0]['text'])
                    self.assertIn('Ownership/membership conflict',output[-1]['result']['content'][0]['text'])
            dispatched=[rpc['params']['name'] for rpc in calls if rpc['method']=='tools/call']
            self.assertEqual([name for name in dispatched if name!='run'],
                ['tabs','windows','tab_groups','snapshot','read','grep','diff','screenshot'])
            self.assertNotIn('new',[rpc.get('params',{}).get('arguments',{}).get('action') for rpc in calls])

    async def test_expired_resume_uses_only_proven_protocol_and_never_legacy_takeover(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        for mode in ('accepted','rejected','unsupported','ack-unverified','implicit','unclaimed-no-proof'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                old=dict(pageId=5,tabId=7,targetId='old-target',groupId='saved-group',
                    url='https://example.com/editor',ownership='mine',ownerAgentId=None)
                pages=[dict(old,ownership='other-agent',ownerAgentId='prior-owner')]
                groups=[dict(groupId='saved-group',title='original task')]
                calls=[]
                def handler(request):
                    rpc=json.loads(request.content);calls.append(rpc)
                    if rpc['method']=='ping':return response(request)
                    name=rpc['params']['name'];args=rpc['params']['arguments']
                    if name=='tabs':
                        self.assertEqual(args['action'],'reclaim_expired')
                        self.assertEqual(args['priorSession'],'old-session')
                        self.assertEqual(args['expected'],[dict(targetId='old-target',url=old['url'])])
                        self.assertEqual(args['session'],'new-session')
                        if mode=='rejected':return response(request,dict(isError=True,content=[dict(type='text',text='active owner')]))
                        if mode!='ack-unverified':pages[0]=dict(old)
                        return response(request,dict(content=[dict(type='text',text='verified expired task')]))
                    self.assertEqual(name,'run')
                    value=(dict(pages=pages,groups=groups) if 'Browser.getTabGroups' in args['code'] else pages)
                    return response(request,dict(structuredContent=dict(ok=True,value=value)))
                out=[];b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='new-session'
                b.catalog['catalog']['tools']=[dict(name='tabs',inputSchema=dict(properties=dict(
                    action=dict(enum=['list','new','close']+([] if mode=='unsupported' else ['reclaim_expired'])))))]
                b.task_state.save('old-session',[old],groups)
                if mode=='unclaimed-no-proof':
                    pages[0]=dict(old,ownership='user')
                    b.task_state.record.pop('pages_session')
                request=(dict(name='run',arguments=dict(code='return 1')) if mode=='implicit'
                         else dict(name='workspace_cleanup',arguments=dict(action='resume',task_id=task)))
                await b.handle(dict(jsonrpc='2.0',id=9,method='tools/call',params=request))
                actual=[c['params']['arguments']['action'] for c in calls
                        if c['method']=='tools/call' and c['params']['name']=='tabs']
                self.assertEqual(actual,[] if mode in ('unsupported','implicit','unclaimed-no-proof') else ['reclaim_expired'])
                self.assertEqual(b.task_state.record['pages'][0]['targetId'],'old-target')
                self.assertEqual(b.task_state.record.get('pages_session'),
                    'new-session' if mode=='accepted' else None if mode=='unclaimed-no-proof' else 'old-session')
                if mode=='accepted':self.assertTrue(b.task_prepared)
                elif mode=='implicit':self.assertTrue(out[-1]['result']['isError'])
                else:self.assertEqual(out[-1]['result']['structuredContent']['decision'],'block')

    async def test_restored_resume_needs_explicit_selection_and_keeps_prior_record_on_failure(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        for mode in ('accepted','no-designation','renamed-group','missing-group','ambiguous-groups','rejected','ack-unverified','missing-proof','alias-owner','implicit'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                old=dict(pageId=1,tabId=101,targetId='old-target',groupId='original',
                    url='https://example.com/editor',ownership='mine',ownerAgentId=None)
                pages=[dict(old,pageId=2,tabId=202,targetId='restored-target',groupId='restored',ownership='user')]
                groups=[dict(groupId='restored',title='original task')]
                calls=[]
                def handler(request):
                    rpc=json.loads(request.content);calls.append(rpc)
                    if rpc['method']=='ping':return response(request)
                    name=rpc['params']['name'];args=rpc['params']['arguments']
                    if name=='tabs':
                        self.assertEqual(args['action'],'reclaim_restored')
                        self.assertEqual(args['priorSession'],'old-session')
                        self.assertEqual(args['priorGroupId'],'original')
                        self.assertEqual(args['prior'],[dict(tabId=101,targetId='old-target',url=old['url'])])
                        self.assertEqual(args['expected'],[dict(targetId='restored-target',url=old['url'])])
                        if mode=='rejected':return response(request,dict(isError=True,content=[dict(type='text',text='not normally closed')]))
                        if mode=='accepted':pages[0]['ownership']='mine'
                        return response(request,dict(content=[dict(type='text',text='recovery ACK')]))
                    self.assertEqual(name,'run')
                    value=dict(pages=pages,groups=groups) if 'Browser.getTabGroups' in args['code'] else pages
                    return response(request,dict(structuredContent=dict(ok=True,value=value)))
                out=[];b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='new-session'
                b.catalog['catalog']['tools']=[dict(name='tabs',inputSchema=dict(properties=dict(
                    action=dict(enum=['list','new','close','reclaim_expired','reclaim_restored']))))]
                b.task_state.save('old-session',[old],[dict(groupId='original',title='original task')])
                if mode=='renamed-group':groups[0]['title']='renamed by user'
                if mode=='missing-group':pages.clear();groups.clear()
                if mode=='ambiguous-groups':
                    groups.append(dict(groupId='second',title='original task'))
                    pages.append(dict(pages[0],pageId=8,tabId=303,targetId='second-target',groupId='second'))
                if mode=='missing-proof':b.task_state.record.pop('pages_session')
                if mode=='alias-owner':pages.append(dict(pages[0],pageId=3,ownership='other-agent',ownerAgentId='active-same-client'))
                args=dict(action='resume',task_id=task)
                if mode not in ('no-designation','renamed-group','missing-group','ambiguous-groups','implicit'):
                    args['recovery']=dict(prior_group_id='original',group_id='restored',
                        pages=[dict(targetId='restored-target',url=old['url'])],reason='Human selected this restored group after normal restart')
                request=dict(name='workspace_cleanup',arguments=args)
                if mode=='implicit':request=dict(name='run',arguments=dict(code='return 1'))
                await b.handle(dict(jsonrpc='2.0',id=10,method='tools/call',params=request))
                actual=[c['params']['arguments']['action'] for c in calls
                    if c['method']=='tools/call' and c['params']['name']=='tabs']
                self.assertEqual(actual,['reclaim_restored'] if mode in ('accepted','rejected','ack-unverified') else [])
                self.assertNotIn('new',actual)
                self.assertEqual(b.task_state.record['pages'][0]['targetId'],
                    'restored-target' if mode=='accepted' else 'old-target')
                if mode=='accepted':self.assertTrue(b.task_prepared)
                elif mode=='implicit':self.assertTrue(out[-1]['result']['isError'])
                else:self.assertEqual(out[-1]['result']['structuredContent']['decision'],'block')
                if mode in ('no-designation','renamed-group','missing-group','ambiguous-groups'):
                    self.assertIn('read-only restored candidates',str(out[-1]))

    async def test_cleanup_resume_cancellation_cancels_the_actual_internal_waiter(self):
        from unittest.mock import AsyncMock
        bridge,out=await self.make_bridge(lambda request:self.fail('must not forward a mismatched external request id'))
        entered=asyncio.Event()
        async def wait(message):
            entered.set();await asyncio.Event().wait()
        bridge.cleanup_call=AsyncMock(side_effect=wait)
        task=asyncio.create_task(bridge.handle(dict(jsonrpc='2.0',id=70,method='tools/call',
            params=dict(name='workspace_cleanup',arguments=dict(action='resume',task_id='00000000-0000-4000-8000-000000000001')))))
        await entered.wait()
        await bridge.handle(dict(jsonrpc='2.0',method='notifications/cancelled',params=dict(requestId=70)))
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertNotIn(70,bridge.active_tools)

    async def test_explicit_session_is_reconciled_before_dispatch(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            dispatched=[]
            def handler(request):
                rpc=json.loads(request.content)
                if rpc['method']=='ping':return response(request)
                params=rpc['params'];args=params['arguments']
                if params['name']=='navigate':
                    dispatched.append(args)
                    return response(request,{'_meta':{SESSION_META_KEY:'new-session'}})
                ownership='mine' if args.get('session')=='old-session' else 'other-agent'
                page=dict(pageId=1,targetId='one',groupId='original',url='https://example.com/a',ownership=ownership,ownerAgentId=None)
                return response(request,{'structuredContent':{'ok':True,'value':dict(pages=[page],groups=[dict(id='original',title='task')])}})
            out=[];b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='old-session'
            b.task_state.save('old-session',[dict(targetId='one',groupId='original',url='https://example.com/a',ownership='mine')],[dict(id='original',title='task')])
            await b.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{
                'name':'navigate','arguments':{'session':'new-session','page':1,'url':'https://example.com/b'}}})
            self.assertEqual(dispatched,[])
            self.assertIn('NEO_NOT_READY',out[-1]['result']['content'][0]['text'])
            self.assertEqual(b.task_state.record['pages'][0]['targetId'],'one')

    async def test_post_action_owner_loss_preserves_record_and_original_result(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        for replacement in (False,True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                acted=False;action_count=0
                def handler(request):
                    nonlocal acted,action_count
                    rpc=json.loads(request.content)
                    if rpc['method']=='ping':return response(request)
                    args=rpc['params']['arguments']
                    if rpc['params']['name']=='navigate':
                        acted=True;action_count+=1
                        result={'content':[{'type':'text','text':'original action ack'}]}
                        if replacement:result['_meta']={SESSION_META_KEY:'replacement'}
                        return response(request,result)
                    page=dict(pageId=1,targetId='one',groupId='original',url='https://example.com/a',ownership='other-agent' if acted else 'mine',ownerAgentId=None)
                    return response(request,{'structuredContent':{'ok':True,'value':dict(pages=[page],groups=[dict(id='original',title='task')])}})
                out=[];b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='old-session'
                b.task_state.save('old-session',[dict(targetId='one',groupId='original',url='https://example.com/a',ownership='mine')],[dict(id='original',title='task')])
                await b.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'navigate','arguments':{'page':1,'url':'https://example.com/b'}}})
                self.assertEqual(action_count,1)
                self.assertEqual(out[-1]['result']['content'][0]['text'],'original action ack')
                self.assertTrue(any('WORKSPACE_TRACKING_FAILED' in c.get('text','') for c in out[-1]['result']['content']))
                self.assertEqual(b.task_state.record['pages'][0]['targetId'],'one')
                self.assertFalse(b.task_prepared)

    async def test_finish_rechecks_ownership_after_prepared_state(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            p=dict(pageId=1,targetId='one',groupId='original',url='https://example.com/a',ownership='other-agent',ownerAgentId='active')
            def handler(request):
                rpc=json.loads(request.content)
                value=dict(pages=[p],groups=[dict(id='original',title='task')]) if 'Browser.getTabGroups' in rpc['params']['arguments']['code'] else [p]
                return response(request,{'structuredContent':{'ok':True,'value':value}})
            out=[];b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='old-session';b.task_prepared=True
            b.task_state.save('old-session',[dict(p,ownership='mine')],[dict(id='original',title='task')])
            await b.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':dict(action='check',task_id=task)}})
            self.assertEqual(out[-1]['result']['structuredContent'].get('decision'),'block')
            self.assertEqual(b.task_state.record['pages'][0]['targetId'],'one')

    async def test_invalid_cleanup_fields_do_not_bind_or_contact_browser(self):
        def handler(request):
            self.fail('Malformed local cleanup must not contact the browser')
        bridge, output = await self.make_bridge(handler)
        task_id = '00000000-0000-4000-8000-000000000001'
        for malformed in ({'page_id':9}, {'pages':[9]}, {'help':True}, {'session':'old'}):
            await bridge.handle({'jsonrpc':'2.0','id':7,'method':'tools/call','params':{
                'name':'workspace_cleanup','arguments':{'action':'retain','task_id':task_id,**malformed}}})
            self.assertIsNone(bridge.cleanup.task_id)
            self.assertEqual(output[-1]['result']['structuredContent']['decision'],'block')
            self.assertIn('pageIds=[integer pageId]',output[-1]['result']['structuredContent']['reason'])
        await bridge.handle({'jsonrpc':'2.0','id':8,'method':'tools/call','params':{
            'name':'workspace_cleanup','arguments':{'action':'begin','task_id':task_id}}})
        self.assertTrue(output[-1]['result']['structuredContent']['taskBound'])

    async def test_cold_discovery_does_not_start_runtime_until_browser_call(self):
        state = {"running": False, "starts": 0}
        async def starter():
            state.update(running=True, starts=state["starts"] + 1)
        def handler(request):
            if not state["running"]:
                raise httpx.ConnectError("refused", request=request)
            message = json.loads(request.content)
            if message["method"] == "initialize":
                return response(request, {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "official"}})
            return response(request, {"content": [{"type":"text","text":"ok"}]})
        bridge, output = await self.make_bridge(handler, starter)
        await bridge.handle(INIT)
        await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        await bridge.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        await bridge.handle({'jsonrpc':'2.0','id':3,'method':'ping'})
        self.assertEqual(state["starts"], 0)
        self.assertEqual(output[0]["result"]["serverInfo"]["name"], "browseros-neo")
        self.assertIn('run', [t['name'] for t in output[1]['result']['tools']])
        await bridge.handle(CALL)
        self.assertEqual(state['starts'], 1)
        self.assertFalse(output[-1]['result'].get('isError',False))

    async def test_stateless_to_stateful_preflight_422_reinitializes_before_dispatch(self):
        calls = []
        def handler(request):
            message = json.loads(request.content)
            calls.append(message["method"])
            if message["method"] == "ping":
                return httpx.Response(422, text="Unexpected message, expect initialize request")
            if message["method"] == "initialize":
                return response(request, {"protocolVersion": "2025-06-18"}, session="renewed")
            return response(request, {"content": [{"type": "text", "text": "ok"}]})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize, bridge.session = INIT, None
        await bridge.handle(CALL)
        self.assertEqual(calls, ["ping", "initialize", "tools/call"])
        self.assertFalse(output[-1]["result"].get("isError", False))

    async def test_other_preflight_422_is_not_treated_as_session_expiry(self):
        calls = []
        def handler(request):
            calls.append(json.loads(request.content)["method"])
            return httpx.Response(422, text="invalid request content")
        bridge, output = await self.make_bridge(handler)
        bridge.initialize = INIT
        await bridge.handle(CALL)
        self.assertEqual(calls, ["ping"])
        self.assertIn("NEO_NOT_READY", output[-1]["result"]["content"][0]["text"])

    async def test_dispatched_initialize_required_error_never_replays_tool(self):
        calls = []
        def handler(request):
            method = json.loads(request.content)["method"]
            calls.append(method)
            if method == "ping":
                return response(request)
            return httpx.Response(422, text="Unexpected message, expect initialize request")
        bridge, output = await self.make_bridge(handler)
        bridge.initialize = INIT
        await bridge.handle(CALL)
        self.assertEqual(calls, ["ping", "tools/call"])
        self.assertIn("OUTCOME_UNKNOWN", output[-1]["result"]["content"][0]["text"])

    async def test_503_while_starting_is_retried_only_for_initialization(self):
        state = {"ready": False}
        async def starter():
            state["ready"] = True
        def handler(request):
            return response(request, {"protocolVersion": "2025-03-26"}, status=200 if state["ready"] else 503)
        bridge, output = await self.make_bridge(handler, starter)
        result = await bridge.connect(INIT)
        self.assertIn("result", result)

    async def test_preflight_reconnects_expired_session_before_one_tool_dispatch(self):
        calls = []
        def handler(request):
            message = json.loads(request.content)
            calls.append(message["method"])
            if message["method"] == "ping":
                return response(request, status=404)
            if message["method"] == "initialize":
                return response(request, {"protocolVersion": "2025-03-26"}, session="replacement")
            self.assertNotIn("Mcp-Session-Id", request.headers)
            self.assertEqual(request.headers["MCP-Protocol-Version"], TOOL_PROTOCOL)
            return response(request, {"content": [{"type": "text", "text": "ok"}], "_meta": {"official-session": "preserved"}})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize, bridge.session = INIT, "old"
        await bridge.handle(CALL)
        self.assertEqual(calls, ["ping", "initialize", "tools/call"])
        self.assertEqual(output[-1]["result"]["_meta"], {"official-session": "preserved"})

    async def test_response_loss_after_commit_never_replays_tool(self):
        submissions = []
        def handler(request):
            message = json.loads(request.content)
            if message["method"] == "ping":
                return response(request)
            submissions.append(message)
            raise httpx.ReadError("reply lost after commit", request=request)
        bridge, output = await self.make_bridge(handler)
        bridge.initialize = INIT
        await bridge.handle(CALL)
        self.assertEqual(len(submissions), 1)
        self.assertTrue(output[-1]["result"]["isError"])
        self.assertIn("OUTCOME_UNKNOWN", output[-1]["result"]["content"][0]["text"])

    async def test_launcher_failure_dispatches_no_tool(self):
        calls = []
        async def starter():
            raise RuntimeError("signed launcher unavailable")
        def handler(request):
            calls.append(json.loads(request.content)["method"])
            raise httpx.ConnectError("refused", request=request)
        bridge, output = await self.make_bridge(handler, starter)
        bridge.initialize = INIT
        await bridge.handle(CALL)
        self.assertNotIn("tools/call", calls)
        self.assertIn("NEO_NOT_READY", output[-1]["result"]["content"][0]["text"])

    async def test_sse_keepalive_notifications_and_result_metadata_survive(self):
        async def handler(request):
            rpc = json.loads(request.content)
            data = 'data: \nid: 0\n\n: comment\n\ndata: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n\n'
            data += "data: " + json.dumps({"jsonrpc": "2.0", "id": rpc["id"], "result": {"protocolVersion": "2025-03-26", "_meta": {"a": 1}}}) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream", "Mcp-Session-Id": "real-session"}, content=data)
        bridge, output = await self.make_bridge(handler)
        result = await bridge.connect(INIT)
        self.assertEqual(bridge.session, "real-session")
        self.assertEqual(output[0]["method"], "notifications/tools/list_changed")
        self.assertEqual(result['result']["_meta"], {"a": 1})

    async def test_heartbeat_does_not_reopen_closed_browser(self):
        async def starter():
            self.fail("Heartbeat must not start neo")
        def handler(request):
            self.fail("Heartbeat must stay local to the live stdio transport")
        bridge, output = await self.make_bridge(handler, starter)
        await bridge.handle({"jsonrpc": "2.0", "id": 99, "method": "ping"})
        self.assertEqual(output, [{"jsonrpc": "2.0", "id": 99, "result": {}}])

    async def test_incomplete_initialization_is_retried_without_old_session(self):
        calls = []
        async def starter():
            pass
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("server still starting", request=request)
            self.assertNotIn("Mcp-Session-Id", request.headers)
            return response(request, {"protocolVersion": "2025-03-26"})
        bridge, output = await self.make_bridge(handler, starter)
        result = await bridge.connect(INIT)
        self.assertEqual(len(calls), 2)
        self.assertIn("result", result)
    async def test_browser_session_survives_http_expiry_and_explicit_new_connection(self):
        observed = []
        expired = False
        def handler(request):
            nonlocal expired
            rpc = json.loads(request.content)
            if rpc["method"] == "ping" and expired:
                expired = False
                return response(request, status=404)
            if rpc["method"] == "initialize":
                return response(request, {"protocolVersion": "2025-03-26"}, session="new-http")
            if rpc["method"] == "ping":
                return response(request)
            self.assertNotIn("Mcp-Session-Id", request.headers)
            self.assertEqual(request.headers["Mcp-Method"], "tools/call")
            self.assertEqual(request.headers["Mcp-Name"], "run")
            self.assertEqual(rpc["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"], TOOL_PROTOCOL)
            self.assertEqual(rpc["params"]["arguments"]["agentName"], "codex-test")
            observed.append(rpc["params"]["arguments"].get("session"))
            return response(request, {"content": [], "_meta": {SESSION_META_KEY: "browser-original"}})
        bridge, output = await self.make_bridge(handler)
        await bridge.handle(INIT)
        await bridge.handle(CALL)
        expired = True
        await bridge.handle(dict(CALL, id=5))
        second, _ = await self.make_bridge(handler)
        await second.handle(INIT)
        resume = dict(CALL, params={"name": "run", "arguments": {"code": "return 1", "session": "browser-original"}})
        await second.handle(resume)
        self.assertEqual(observed, [None, "browser-original", "browser-original"])
        self.assertEqual(output[-1]["result"]["_meta"][SESSION_META_KEY], "browser-original")
        self.assertNotIn("session", CALL["params"]["arguments"])

    async def test_concurrent_first_calls_share_one_browser_handle(self):
        handles = []
        async def handler(request):
            rpc = json.loads(request.content)
            if rpc["method"] == "ping":
                return response(request)
            handles.append(rpc["params"]["arguments"].get("session"))
            await asyncio.sleep(0)
            return response(request, {"_meta": {SESSION_META_KEY: "one-browser"}})
        bridge, _ = await self.make_bridge(handler)
        bridge.initialize = INIT
        await asyncio.gather(bridge.handle(CALL), bridge.handle(dict(CALL, id=5)))
        self.assertEqual(handles, [None, "one-browser"])

    async def test_stopped_handle_is_not_cleared_or_automatically_replaced(self):
        seen = []
        def handler(request):
            rpc = json.loads(request.content)
            if rpc["method"] == "ping":
                return response(request)
            seen.append(rpc["params"]["arguments"].get("session"))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"],
                "error": {"code": -32602, "message": "Session was stopped by the operator"}})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize, bridge.tool_session = INIT, "stopped-browser"
        await bridge.handle(CALL)
        self.assertEqual(seen, ["stopped-browser"])
        self.assertEqual(bridge.tool_session, "stopped-browser")
        self.assertIn("stopped", output[-1]["error"]["message"])

    async def test_cancel_notification_closes_only_matching_tool_stream(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        async def handler(request):
            rpc = json.loads(request.content)
            if rpc["method"] == "ping":
                return response(request)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        bridge, _ = await self.make_bridge(handler)
        bridge.initialize = INIT
        pending = asyncio.create_task(bridge.handle(CALL))
        await asyncio.wait_for(started.wait(), 1)
        await bridge.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4}})
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(cancelled.is_set())
        self.assertEqual(bridge.active_tools, {})

    async def test_cleanup_without_browser_use_never_starts_or_calls_upstream(self):
        def handler(request):
            self.fail("Unused browser must not be contacted")
        bridge, output = await self.make_bridge(handler)
        await bridge.handle({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"workspace_cleanup","arguments":{"action":"check","task_id":"00000000-0000-4000-8000-000000000001"}}})
        self.assertFalse(output[-1]["result"]["structuredContent"]["browserUsed"])

    async def test_cleanup_uses_existing_browser_handle_and_returns_hook_json(self):
        calls=[]
        def handler(request):
            rpc=json.loads(request.content);calls.append(rpc)
            return response(request,{"structuredContent":{"ok":True,"value":[{"pageId":7,"targetId":"physical-a","url":"about:blank","ownership":"mine"}]}})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize,bridge.tool_session=INIT,"existing-browser"
        await bridge.handle({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"workspace_cleanup","arguments":{"action":"check","task_id":"00000000-0000-4000-8000-000000000001"}}})
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]["params"]["arguments"]["session"],"existing-browser")
        value=json.loads(output[-1]["result"]["content"][0]["text"])
        self.assertEqual(value["decision"],"block")
        self.assertEqual(value["pendingPageIds"],[7])

    async def test_stop_requires_fresh_retention_at_next_boundary(self):
        pages=[{"pageId":7,"targetId":"physical-a","url":"about:blank","ownership":"mine"}]
        def handler(request):
            return response(request,{"structuredContent":{"ok":True,"value":pages}})
        bridge,output=await self.make_bridge(handler)
        bridge.initialize,bridge.tool_session=INIT,"existing-browser"
        async def cleanup(action="check",**kwargs):
            await bridge.handle({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{
                "name":"workspace_cleanup","arguments":dict(action=action,
                    task_id="00000000-0000-4000-8000-000000000001",**kwargs)}})
            return output[-1]["result"]["structuredContent"]
        await cleanup("retain",pageIds=[7],reason="Approved next inspection is pending")
        self.assertNotIn("decision",await cleanup(stop_hook_active=False))
        stale=await cleanup(stop_hook_active=False)
        self.assertEqual(stale["reviewPageIds"],[7])
        self.assertEqual(stale["decision"],"block")
        warning=await cleanup(stop_hook_active=True)
        self.assertNotIn("decision",warning)
        self.assertIn("systemMessage",warning)
        await cleanup("release",pageIds=[7])
        pages.clear()
        self.assertEqual((await cleanup(stop_hook_active=True))["pendingPageIds"],[])
        self.assertEqual(bridge.cleanup.retained,{})
        self.assertEqual(bridge.cleanup.review_required,set())

    async def test_cleanup_replaced_session_blocks_and_is_never_replayed(self):
        calls=[]
        def handler(request):
            calls.append(json.loads(request.content))
            return response(request,{"_meta":{SESSION_META_KEY:"replacement"},"structuredContent":{"ok":True,"value":[]}})
        bridge,output=await self.make_bridge(handler)
        bridge.initialize,bridge.tool_session=INIT,"prior-session"
        await bridge.handle({"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"workspace_cleanup","arguments":{"action":"check","task_id":"00000000-0000-4000-8000-000000000001"}}})
        self.assertEqual(len(calls),1)
        self.assertIn("Target.getTargets",calls[0]["params"]["arguments"]["code"])
        value=output[-1]["result"]["structuredContent"]
        self.assertEqual(value["decision"],"block")
        self.assertIn("Reconcile",value["reason"])

    async def test_invalid_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            Bridge("https://remote.invalid/mcp", lambda _: None)

    async def test_offline_check_is_unverified_without_stop_continuation_or_state_loss(self):
        import copy
        import tempfile
        task_id = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            for active in (False, True):
                with self.subTest(stop_hook_active=active):
                    requests = []
                    def handler(request):
                        requests.append(request)
                        raise httpx.ConnectError('refused', request=request)
                    async def starter():
                        self.fail('An offline Stop check must never start Neo')
                    output = []
                    bridge = Bridge('http://127.0.0.1:9010/mcp', output.append,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        starter=starter, state_root=root, task_id=task_id)
                    self.addAsyncCleanup(bridge.close)
                    bridge.initialize, bridge.tool_session = INIT, 'prior-browser'
                    pages = [dict(pageId=7, tabId=7, targetId='saved-target', groupId='saved-group',
                                  url='https://example.com/draft', ownership='mine')]
                    retained = [(('prior-browser', 'saved-target', 'https://example.com/draft'),
                                 'Unconfirmed saved draft needs inspection')]
                    bridge.task_state.record.update(execution_incomplete=True, finish_checked_execution=None)
                    bridge.task_state.save('prior-browser', pages,
                        [dict(id='saved-group', title='verified task')], retained=retained)
                    bridge.cleanup.retained = dict(retained)
                    bridge.cleanup.review_required = set(bridge.cleanup.retained)
                    before = copy.deepcopy(bridge.task_state.record)
                    path = Path(root) / (task_id + '.json')
                    raw = path.read_bytes()
                    await bridge.handle({'jsonrpc':'2.0','id':9,'method':'tools/call','params':{
                        'name':'workspace_cleanup','arguments':{'action':'check', 'task_id':task_id,
                            'stop_hook_active':active}}})
                    value = output[-1]['result']['structuredContent']
                    self.assertEqual(json.loads(output[-1]['result']['content'][0]['text']),
                                     {'suppressOutput': True})
                    self.assertEqual(value['cleanupStatus'], 'unverified')
                    self.assertFalse(value['cleanupVerified'])
                    self.assertIn('ConnectError', value['diagnostic'])
                    for key in ('decision', 'reason', 'systemMessage', 'pendingPageIds', 'retained', 'browserClosed'):
                        self.assertNotIn(key, value)
                    self.assertEqual(bridge.task_state.record, before)
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(bridge.cleanup.retained, dict(retained))
                    self.assertEqual(bridge.cleanup.review_required, set(dict(retained)))
                    self.assertIsNone(bridge.cleanup_closed_session)
                    self.assertEqual(bridge.tool_session, 'prior-browser')
                    self.assertEqual(len(requests), 1)

    async def test_only_check_connect_error_is_nonblocking(self):
        task_id = '00000000-0000-4000-8000-000000000001'
        for action, error_type in (('retain', httpx.ConnectError), ('release', httpx.ConnectError),
                                   ('check', httpx.ReadTimeout), ('check', httpx.ReadError)):
            with self.subTest(action=action, error_type=error_type):
                def handler(request):
                    raise error_type('unavailable', request=request)
                bridge, output = await self.make_bridge(handler)
                bridge.initialize, bridge.tool_session = INIT, 'prior-browser'
                await bridge.handle({'jsonrpc':'2.0','id':9,'method':'tools/call','params':{
                    'name':'workspace_cleanup','arguments':{'action':action, 'task_id':task_id}}})
                self.assertEqual(output[-1]['result']['structuredContent']['decision'], 'block')

    async def test_cleanup_after_verified_empty_and_native_close_never_contacts_off_browser(self):
        calls = []
        closed = False
        def handler(request):
            nonlocal closed
            self.assertFalse(closed, "Stop must not contact a normally closed browser")
            rpc = json.loads(request.content); calls.append(rpc)
            if rpc['method'] == 'ping':
                return response(request)
            if rpc['params']['name'] == 'windows':
                closed = True
                return response(request, {'content':[{'type':'text','text':'closed window 42'}]})
            return response(request, {'structuredContent':{'ok':True,'value':[]}})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize, bridge.tool_session = INIT, 'existing-browser'
        check = {'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':{'action':'check','task_id':'00000000-0000-4000-8000-000000000001'}}}
        await bridge.handle(check)
        await bridge.handle({'jsonrpc':'2.0','id':10,'method':'tools/call','params':{'name':'windows','arguments':{'action':'close','windowId':42}}})
        count = len(calls)
        await bridge.handle(check)
        self.assertEqual(len(calls), count)
        self.assertEqual(output[-1]['result']['structuredContent'], {'pendingPageIds':[],'retained':[],'browserClosed':True})

    async def test_close_receipt_requires_empty_proof_exact_ack_and_same_session(self):
        for kind in ('owned-page', 'missing-ack', 'other-session'):
            with self.subTest(kind=kind):
                closed = False
                def handler(request):
                    nonlocal closed
                    if closed:
                        raise httpx.ConnectError('refused', request=request)
                    rpc = json.loads(request.content)
                    if rpc['method'] == 'ping': return response(request)
                    if rpc['params']['name'] == 'windows':
                        closed = True
                        return response(request, {'content':[{'type':'text','text':'unknown' if kind=='missing-ack' else 'closed window 42'}]})
                    pages = [{'pageId':7,'targetId':'physical-a','url':'about:blank','ownership':'mine'}] if kind=='owned-page' else []
                    return response(request, {'structuredContent':{'ok':True,'value':pages}})
                bridge, output = await self.make_bridge(handler)
                bridge.initialize, bridge.tool_session = INIT, 'existing-browser'
                check = {'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':{'action':'check','task_id':'00000000-0000-4000-8000-000000000001'}}}
                await bridge.handle(check)
                args = {'action':'close','windowId':42}
                if kind=='other-session': args['session']='another-browser'
                await bridge.handle({'jsonrpc':'2.0','id':10,'method':'tools/call','params':{'name':'windows','arguments':args}})
                await bridge.handle(check)
                value = output[-1]['result']['structuredContent']
                self.assertEqual(value['cleanupStatus'], 'unverified')
                self.assertFalse(value['cleanupVerified'])
                self.assertNotIn('browserClosed', value)
                self.assertIsNone(bridge.cleanup_closed_session)

    async def test_new_browser_work_invalidates_close_receipt(self):
        def handler(request):
            rpc = json.loads(request.content)
            if rpc['method'] == 'ping': return response(request)
            return response(request, {'structuredContent':{'ok':True,'value':[{'pageId':7,'targetId':'physical-a','url':'about:blank','ownership':'mine'}]}})
        bridge, output = await self.make_bridge(handler)
        bridge.initialize, bridge.tool_session = INIT, 'existing-browser'
        bridge.cleanup_closed_session = 'existing-browser'
        await bridge.handle(CALL)
        self.assertIsNone(bridge.cleanup_closed_session)
        await bridge.handle({'jsonrpc':'2.0','id':9,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':{'action':'check','task_id':'00000000-0000-4000-8000-000000000001'}}})
        self.assertEqual(output[-1]['result']['structuredContent']['pendingPageIds'],[7])


@unittest.skipUnless(sys.platform == "win32", "Windows internal wakeup channel")
class WindowsLoopTests(unittest.TestCase):
    def test_closed_internal_socket_exits_instead_of_spinning(self):
        # Use a real Windows loop and a closed peer, the observed failure path.
        # Run out of process because the guard must terminate blocked-stdin threads.
        program = """
import asyncio, socket
from neo_mcp_bridge import GuardedProactorEventLoop
async def fail_channel():
    loop = asyncio.get_running_loop()
    loop._csock.shutdown(socket.SHUT_RDWR)
    loop._csock.close()
    await asyncio.sleep(2)
with asyncio.Runner(loop_factory=GuardedProactorEventLoop) as runner:
    runner.run(fail_channel())
"""
        result = subprocess.run([sys.executable, "-B", "-c", program],
                                cwd=Path(neo_mcp_bridge.__file__).parent,
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn(b"NEO_LOOP_CHANNEL_CLOSED", result.stderr)
        self.assertEqual(result.stdout, b"")


class OutputBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_local_outputs_entrypoint_never_contacts_or_starts_browser(self):
        import tempfile, time, uuid
        from neo_task_state import TaskState
        task = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            root = Path(directory)
            output_root = root / 'tool-output'; output_root.mkdir()
            state = TaskState(root / 'tasks', output_root=output_root); state.bind(task)
            dispatched = time.time_ns()
            path = output_root / f'evaluate-{time.time_ns()//1000000}-{uuid.uuid4()}.txt'
            path.write_text('synthetic local result', encoding='utf-8')
            state.record_output('evaluate', {'structuredContent':dict(writtenToFile=True,path=str(path),contentLength=22)}, dispatched, 'direct-1', 'old', frozenset())
            async def starter(): self.fail('outputs must never start Neo')
            def handler(request): self.fail('outputs must never call HTTP')
            responses = []
            bridge = Bridge('http://127.0.0.1:9010/mcp', responses.append,
                            state_root=root/'tasks', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), starter=starter)
            self.addAsyncCleanup(bridge.close)
            bridge.task_state.output_root = output_root
            before = (root/'tasks'/(task+'.json')).read_bytes()
            await bridge.handle({'jsonrpc':'2.0','id':10,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':{'action':'outputs','task_id':task}}})
            value = responses[-1]['result']['structuredContent']
            self.assertEqual(value['outputs'][0]['status'], 'present')
            self.assertEqual(value['present_bytes'], path.stat().st_size)
            self.assertEqual((root/'tasks'/(task+'.json')).read_bytes(), before)
            self.assertTrue(path.exists())

    async def test_mocked_http_direct_spill_tracks_and_save_failure_preserves_original_response(self):
        import tempfile, time, uuid
        from unittest.mock import AsyncMock, patch
        task = '00000000-0000-4000-8000-000000000001'
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            root = Path(directory); output_root = root/'tool-output'; output_root.mkdir()
            calls = []
            def handler(request):
                rpc = json.loads(request.content); calls.append(rpc)
                path = output_root / f'evaluate-{time.time_ns()//1000000}-{uuid.uuid4()}.txt'
                path.write_text('synthetic upstream result', encoding='utf-8')
                return response(request, dict(content=[dict(type='text',text='original response')],
                    structuredContent=dict(writtenToFile=True,path=str(path),contentLength=25,page=1)))
            out=[]
            bridge = Bridge('http://127.0.0.1:9010/mcp', out.append,state_root=root/'tasks',task_id=task,
                            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            self.addAsyncCleanup(bridge.close)
            bridge.task_state.output_root=output_root
            bridge.ensure_ready=AsyncMock(); bridge.prepare_task=AsyncMock(); bridge.remember_task=AsyncMock()
            bridge.tool_session='session-owned'
            message={'jsonrpc':'2.0','id':11,'method':'tools/call','params':{'name':'evaluate','arguments':{'page':1,'code':'return synthetic'}}}
            await bridge.handle(message)
            self.assertEqual(len(calls),1)
            self.assertEqual(len(bridge.task_state.record['outputs']),1)
            self.assertEqual(out[-1]['result']['content'][0]['text'],'original response')
            import neo_task_state
            actual_write=neo_task_state.atomic_write_json
            def fail_new_output(path,record,**kwargs):
                if len(record.get('outputs',[]))>1:
                    raise OSError('synthetic post-dispatch disk failure')
                return actual_write(path,record,**kwargs)
            with patch('neo_task_state.atomic_write_json', side_effect=fail_new_output):
                await bridge.handle(dict(message,id=12))
            self.assertEqual(len(calls),2)
            self.assertEqual(out[-1]['result']['content'][0]['text'],'original response')
            self.assertTrue(out[-1]['result']['structuredContent']['writtenToFile'])
            self.assertIn('WORKSPACE_OUTPUT_TRACKING_FAILED',out[-1]['result']['content'][1]['text'])
            self.assertEqual(len(bridge.task_state.record['outputs']),1)
            with patch.object(bridge.task_state, 'output_baseline', side_effect=OSError('synthetic baseline denial')):
                await bridge.handle(dict(message,id=13))
            self.assertEqual(len(calls),3)
            self.assertEqual(out[-1]['result']['content'][0]['text'],'original response')
            self.assertIn('synthetic baseline denial',out[-1]['result']['content'][1]['text'])
            self.assertEqual(len(bridge.task_state.record['outputs']),1)
            with patch('neo_task_state.atomic_write_json', side_effect=OSError('synthetic pre-dispatch disk failure')):
                await bridge.handle(dict(message,id=14))
            self.assertEqual(len(calls),3)
            self.assertTrue(out[-1]['result']['isError'])
            self.assertIn('NOT dispatched',out[-1]['result']['content'][0]['text'])


if __name__ == "__main__":
    unittest.main()


class UsedBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_allows_use_defer_check_without_claiming_or_losing_prior_bindings(self):
        import tempfile
        from neo_cleanup import has_pending_cleanup
        task='00000000-0000-4000-8000-000000000001'
        current=dict(pageId=5,tabId=7,targetId='prior',groupId='prior-group',
            url='https://example.com/editor',ownership='other-agent',ownerAgentId='unknown-owner')
        groups=[dict(groupId='prior-group',title='recorded task')]
        calls=[]
        def handler(request):
            rpc=json.loads(request.content);calls.append(rpc)
            if rpc['method']=='ping':
                return response(request)
            self.assertEqual(rpc['params']['name'],'run')
            code=rpc['params']['arguments']['code']
            value=(dict(tabs=[dict(tabId=7,targetId='prior',url=current['url'])],
                        targets=['prior'],pages=[current]) if 'Browser.getTabs' in code
                   else dict(pages=[current],groups=groups) if 'Browser.getTabGroups' in code else [current])
            return response(request,dict(structuredContent=dict(ok=True,value=value)))
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            out=[];bridge=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                catalog={'catalog':{'tools':[]}})
            self.addAsyncCleanup(bridge.close);bridge.initialize=INIT;bridge.tool_session='new-session'
            retained=[(('prior-session','prior',current['url']),'Unfinished real work needs this exact editor')]
            bridge.task_state.save('new-session',[dict(current,ownership='mine')],groups,retained)
            bridge.cleanup.retained={tuple(k):v for k,v in retained}
            bridge.execution_id='current-execution'
            before=json.loads((Path(root)/(task+'.json')).read_text(encoding='utf-8'))
            async def invoke(action,**kw):
                await bridge.handle(dict(jsonrpc='2.0',id=10,method='tools/call',params=dict(
                    name='workspace_cleanup',arguments=dict(action=action,task_id=task,**kw))))
                self.assertNotIn('error',out[-1]);self.assertFalse(out[-1]['result'].get('isError',False))
                return out[-1]['result']['structuredContent']
            used=await invoke('use',pageIds=[5],reason='Read the existing approved editor without changing ownership')
            self.assertEqual(used['pendingUsedTargetIds'],['prior'])
            deferred=await invoke('defer',pageIds=[5],condition='other_owner',
                reason='Original owner recovery unresolved; preserve unfinished work')
            self.assertEqual(deferred['pendingUsedTargetIds'],[])
            self.assertEqual(len(deferred['deferredUsedPages']),1)
            checked=await invoke('check',stop_hook_active=True)
            self.assertTrue(checked['pendingReconciliationGroups'])
            self.assertTrue(has_pending_cleanup(checked));self.assertEqual(checked['decision'],'block')
            after=json.loads((Path(root)/(task+'.json')).read_text(encoding='utf-8'))
            for key in ('pages','groups','retained','session'):
                self.assertEqual(after.get(key),before.get(key))
            self.assertTrue(after['execution_incomplete'])
            self.assertIsNone(after['finish_checked_execution'])
            self.assertIsNone(bridge.cleanup_empty_session)
            self.assertFalse(bridge.task_prepared)
            # Resume and edits must still meet the unchanged ownership guard.
            await bridge.handle(dict(jsonrpc='2.0',id=11,method='tools/call',params=dict(
                name='workspace_cleanup',arguments=dict(action='resume',task_id=task))))
            self.assertIn('ownership reconciliation',json.dumps(out[-1]))
            self.assertFalse(any(c.get('params',{}).get('name')=='tabs' for c in calls))

    async def test_durable_use_survives_reconnect_alias_loss_and_only_absence_clears_it(self):
        import tempfile
        task='00000000-0000-4000-8000-000000000001'
        pages=[dict(pageId=5,tabId=7,targetId='used',url='https://stale.example/',ownership='user',ownerAgentId=None)]
        tabs=[dict(tabId=7,targetId='used',url='https://example.com/editor')]
        targets=['used'];calls=[]
        def handler(request):
            rpc=json.loads(request.content);calls.append(rpc)
            code=rpc['params']['arguments']['code']
            value=(dict(tabs=tabs,targets=targets,pages=pages) if 'Browser.getTabs' in code
                   else dict(pages=pages,groups=[]) if 'Browser.getTabGroups' in code else pages)
            return response(request,dict(structuredContent=dict(ok=True,value=value)))
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            async def make():
                out=[]; b=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=task,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
                self.addAsyncCleanup(b.close);b.initialize=INIT;b.tool_session='s'
                return b,out
            async def invoke(b,out,action='check',**kw):
                await b.handle(dict(jsonrpc='2.0',id=10,method='tools/call',params=dict(name='workspace_cleanup',arguments=dict(action=action,task_id=task,**kw))))
                return out[-1]['result']['structuredContent']
            b,out=await make(); await invoke(b,out,'use',pageIds=[5],reason='Actual editor reuse for approved task')
            self.assertEqual(len(b.task_state.record['used_tabs']),1)
            fresh,out=await make(); self.assertEqual((await invoke(fresh,out))['pendingUsedTargetIds'],['used'])
            await invoke(fresh,out,'defer',pageIds=[5],condition='permission_missing',reason='User tab close permission absent; preserve document')
            self.assertEqual((await invoke(fresh,out))['cleanupStatus'],'deferred')
            again,out=await make(); self.assertEqual((await invoke(again,out))['decision'],'block')
            pages.clear()
            self.assertEqual((await invoke(again,out))['pendingUsedTargetIds'],['used'])
            tabs.clear();targets.clear()
            result=await invoke(again,out)
            self.assertEqual(result['closedUsedTargetIds'],['used'])
            self.assertEqual(again.task_state.record['used_tabs'],[])
            self.assertFalse(any(c['params']['name']!='run' for c in calls))

    async def test_raw_transport_has_no_task_scope_and_use_cannot_certify_it(self):
        out=[]; b=Bridge('http://127.0.0.1:9010/mcp',out.append)
        self.addAsyncCleanup(b.close);b.tool_session='s'
        await b.handle(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='workspace_cleanup',arguments=dict(action='use',task_id='00000000-0000-4000-8000-000000000001',pageIds=[1],reason='Require durable actual task binding'))))
        self.assertIn('durable bound task',out[-1]['result']['structuredContent']['reason'])
