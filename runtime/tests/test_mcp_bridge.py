import asyncio
import json
import sys
import subprocess
import os
from pathlib import Path
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge, SESSION_META_KEY, TOOL_PROTOCOL
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
    return httpx.Response(status, headers={"Mcp-Session-Id": session}, json={
        "jsonrpc": "2.0", "id": message.get("id"), "result": result or {}})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reclaim_is_internal_and_cannot_dispatch_from_public_tools(self):
        calls = []
        bridge, output = await self.make_bridge(lambda request: calls.append(request))
        bridge.catalog['catalog']['tools'] = [{'name': 'tabs', 'inputSchema': {'properties': {
            'action': {'enum': ['list', 'new', 'reclaim']}, 'expected': {'type': 'array'}}}}]
        await bridge.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        properties = output[-1]['result']['tools'][0]['inputSchema']['properties']
        self.assertEqual(properties['action']['enum'], ['list', 'new'])
        self.assertNotIn('expected', properties)
        self.assertIn('reclaim', bridge.catalog['catalog']['tools'][0]['inputSchema']['properties']['action']['enum'])
        for args in ({'action': 'reclaim', 'groupId': 'foreign'}, {'action': 'new', 'expected': []}):
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
            pages=[dict(pageId=10,tabId=20,targetId='restored',groupId='restored-group',
                        url='https://example.com/login',ownership='user',ownerAgentId=None)]
            groups=[dict(groupId='restored-group',title='recorded task')]
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
            for name,args in (('tabs',{'action':'new'}),('tabs',{'action':'close','page':10}),
                              ('windows',{'action':'close','windowId':1}),
                              ('tabs',{'action':'list','url':'https://example.com'}),
                              ('run',{'code':'return 1'})):
                with self.subTest(blocked=name,args=args):
                    await bridge.handle({'jsonrpc':'2.0','id':21,'method':'tools/call',
                        'params':{'name':name,'arguments':args}})
                    self.assertIn('NEO_NOT_READY',output[-1]['result']['content'][0]['text'])
                    self.assertIn('Ownership/membership conflict',output[-1]['result']['content'][0]['text'])
            dispatched=[rpc['params']['name'] for rpc in calls if rpc['method']=='tools/call']
            self.assertEqual([name for name in dispatched if name!='run'],['tabs','windows','tab_groups'])
            self.assertNotIn('new',[rpc.get('params',{}).get('arguments',{}).get('action') for rpc in calls])

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
            with patch('neo_task_state.atomic_write_json', side_effect=OSError('synthetic disk failure')):
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


if __name__ == "__main__":
    unittest.main()


class UsedBridgeTests(unittest.IsolatedAsyncioTestCase):
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
