import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('agy_runner',Path(__file__).parents[1]/'run-agy-neo.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
TASK='00000000-0000-4000-8000-000000000001'

class FinishTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.plan={'ledger_root':self.temp.name,'mcp_endpoint':'http://127.0.0.1:9010/mcp'}
        self.path=Path(self.temp.name)/'task-tabs/agy'/(TASK+'.json')

    def record(self,**values):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text(json.dumps(values),encoding='utf-8')

    async def test_no_browser_work_never_constructs_upstream_client(self):
        with patch.object(runner,'Bridge',side_effect=AssertionError('no connection')):
            self.assertFalse((await runner.finish_check(self.plan,TASK,'current'))['browserUsed'])
            self.record(execution_id='previous')
            self.assertFalse((await runner.finish_check(self.plan,TASK,'current'))['browserUsed'])

    async def test_same_execution_pending_and_stale_retention_remain_incomplete(self):
        record=dict(execution_id='current',review_required=[['session','target','url']])
        self.record(**record)
        class Fake:
            def __init__(self,endpoint,emit,**kwargs):
                self.emit=emit
                self.cleanup=type('Policy',(),{'review_required':set(),'retained':{('session','target','url'):'unfinished'}})()
            async def handle(self,message):
                self.emit({'result':{'structuredContent':{'pendingPageIds':[7],'reviewPageIds':[8] if self.cleanup.review_required else []}}})
            async def close(self):pass
        with patch.object(runner,'Bridge',Fake):
            check=await runner.finish_check(self.plan,TASK,'current')
            self.assertEqual(check['pendingPageIds'],[7])
            self.assertEqual(check['reviewPageIds'],[8])
            self.record(**record,finish_checked_execution='current')
            self.assertEqual((await runner.finish_check(self.plan,TASK,'current'))['reviewPageIds'],[])

    async def test_unreadable_current_record_is_not_reported_clean(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{',encoding='utf-8')
        with self.assertRaises(ValueError):await runner.finish_check(self.plan,TASK,'current')

    async def test_failed_browser_request_with_no_handle_cannot_finish_as_unused(self):
        self.record(schema_version=1,task_id=TASK,session=None,pages=[],groups=[],retained=[],execution_id='current',execution_incomplete=True)
        check=await runner.finish_check(self.plan,TASK,'current')
        self.assertEqual(check['decision'],'block')
        self.assertIn('failed before ownership inspection',check['reason'])

    async def test_offline_check_cannot_be_reported_as_successful_agy_completion(self):
        import httpx
        self.record(schema_version=1, task_id=TASK, session='prior-browser', pages=[],
                    groups=[], retained=[], execution_id='current', execution_incomplete=True)
        before = self.path.read_bytes()
        def handler(request):
            raise httpx.ConnectError('refused', request=request)
        actual_bridge = runner.Bridge
        def bridge_factory(*args, **kwargs):
            return actual_bridge(*args, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs)
        with patch.object(runner, 'Bridge', bridge_factory):
            check = await runner.finish_check(self.plan, TASK, 'current')
        self.assertFalse(check['cleanupVerified'])
        self.assertEqual(check['cleanupStatus'], 'unverified')
        self.assertEqual(check['decision'], 'block')
        self.assertIn('ConnectError', check['diagnostic'])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_borrowed_scope_blocks_and_same_execution_deferral_is_not_reconsumed(self):
        import httpx
        page=dict(pageId=3,targetId='used',tabId=7,url='about:blank',ownership='other-agent',ownerAgentId='active')
        saved=dict(targetId='used',tabId=7,url='about:blank',purpose='Actual borrowed synthetic editor')
        base=dict(schema_version=1,task_id=TASK,session='s',pages=[],groups=[],retained=[],execution_id='current',used_tabs=[saved])
        def handler(request):
            rpc=json.loads(request.content);code=rpc['params']['arguments']['code']
            value=(dict(tabs=[dict(targetId='used',tabId=7,url='about:blank')],targets=['used'],pages=[page])
                   if 'Browser.getTabs' in code else dict(pages=[page],groups=[]) if 'Browser.getTabGroups' in code else [page])
            return httpx.Response(200,json=dict(jsonrpc='2.0',id=rpc['id'],result=dict(structuredContent=dict(ok=True,value=value))))
        actual=runner.Bridge
        def factory(*args,**kw):return actual(*args,client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),**kw)
        with patch.object(runner,'Bridge',factory):
            self.record(**base)
            blocked=await runner.finish_check(self.plan,TASK,'current')
            self.assertEqual(blocked['decision'],'block')
            self.assertEqual(blocked['pendingPageIds'],[])
            self.assertEqual(blocked['pendingUsedTargetIds'],['used'])
            saved['deferred']=dict(signature=['s','about:blank',7,'other_owner',[['other-agent','active']]],
                                  condition='unsaved',reason='Synthetic document still dirty; preserve exact target',review_required=False)
            self.record(**base,finish_checked_execution='current')
            deferred=await runner.finish_check(self.plan,TASK,'current')
            self.assertEqual(deferred['cleanupStatus'],'deferred')
            self.assertNotIn('decision',deferred)
            self.record(**base,finish_checked_execution='previous')
            self.assertEqual((await runner.finish_check(self.plan,TASK,'current'))['decision'],'block')
