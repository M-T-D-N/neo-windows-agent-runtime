"""Real OS lock behavior plus real Bridge dispatch ordering with mock HTTP."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge,SESSION_META_KEY,INVENTORY_META_KEY
from neo_task_state import TaskState
from neo_operation_boundary import OperationBoundary

TASK='00000000-0000-4000-8000-000000000001'
RESET='00000000-0000-4000-8000-000000000002'

def closed(record):
    return dict(record,browser_reset_id=RESET,browser_reset_verified=True,browser_reset_pending=True,
        browser_closed=True,session=None,pages_session=None,pages=[],groups=[],used_tabs=[],retained=[],
        prior_bindings=[],review_required=[])

def reply(request,result):
    return httpx.Response(200,json=dict(jsonrpc='2.0',id=json.loads(request.content)['id'],
        result=dict(result,_meta={SESSION_META_KEY:'current-session'})))


class KernelBoundaryTests(unittest.TestCase):
    def test_reparse_boundary_is_rejected_before_resolution(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            root=Path(directory);target=root/'target';target.mkdir();link=root/'link'
            if os.name=='nt':
                made=subprocess.run(['cmd.exe','/d','/c','mklink','/J',str(link),str(target)],capture_output=True)
                self.assertEqual(made.returncode,0,made.stderr.decode('utf-8','replace'))
            else:
                link.symlink_to(target,target_is_directory=True)
            try:
                with self.assertRaises((ValueError,OSError)):
                    OperationBoundary(link/'boundary')
                self.assertFalse((target/'boundary').exists())
            finally:
                if os.name=='nt':os.rmdir(link)
                else:link.unlink()

    def test_shared_clients_overlap_and_exclusive_administration_waits(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            p=Path(directory)/'boundary'
            with OperationBoundary(p),OperationBoundary(p):
                with self.assertRaises(TimeoutError):
                    with OperationBoundary(p,exclusive=True,timeout=.03):self.fail('Exclusive overlapped shared')
            with OperationBoundary(p,exclusive=True) as guard:
                guard.require_exclusive(p)
                with self.assertRaises(TimeoutError):
                    with OperationBoundary(p,timeout=.03):self.fail('Shared overlapped administrator')

    def test_separate_process_shared_lock_blocks_administrator(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            root=Path(directory);ready=root/'ready';release=root/'release';gate=root/'boundary'
            code='''from pathlib import Path
import sys,time
from neo_operation_boundary import OperationBoundary
gate,ready,release=map(Path,sys.argv[1:])
with OperationBoundary(gate):
 ready.write_text('held',encoding='utf-8')
 until=time.monotonic()+3
 while not release.exists() and time.monotonic()<until:time.sleep(.01)
'''
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            with subprocess.Popen([sys.executable,'-B','-c',code,str(gate),str(ready),str(release)],
                    env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)) as child:
                try:
                    until=time.monotonic()+2
                    while not ready.exists() and child.poll() is None and time.monotonic()<until:time.sleep(.01)
                    self.assertTrue(ready.exists())
                    self.assertIsNone(child.poll())
                    with self.assertRaises(TimeoutError):
                        with OperationBoundary(gate,exclusive=True,timeout=.03):self.fail('Cross-process overlap')
                finally:
                    release.touch()
                    out,err=child.communicate(timeout=8)
                self.assertEqual(child.returncode,0,err.decode('utf-8','replace'))
            with OperationBoundary(gate,exclusive=True):pass

    def test_administrator_requires_live_exact_exclusive_token(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            state=TaskState(directory);state.bind(TASK);state.save('old',[],[])
            path=Path(directory)/(TASK+'.json');expected=json.loads(path.read_text(encoding='utf-8'))
            for token in (None,state.browser_operation(exclusive=True)):
                with self.assertRaisesRegex(ValueError,'exclusive'):
                    state._write_record(closed(expected),administrator_reset=True,expected_state=expected,operation_guard=token)
            with OperationBoundary(Path(directory)/'wrong',exclusive=True) as wrong:
                with self.assertRaisesRegex(ValueError,'exclusive'):
                    state._write_record(closed(expected),administrator_reset=True,expected_state=expected,operation_guard=wrong)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),expected)
            codex=TaskState(Path(directory)/'task-tabs/codex')
            agy=TaskState(Path(directory)/'task-tabs/agy')
            self.assertEqual(codex.browser_operation().path,agy.browser_operation().path)


class DispatchBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_reset_reused_page_requires_delivered_inventory_and_receipt(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            output=[];calls=[];failed=[False]
            page=dict(pageId=1,targetId='new-user-target',tabId=101,groupId='user-group',
                url='https://example.com/new',ownership='user',ownerAgentId=None)
            def handler(request):
                rpc=json.loads(request.content);calls.append(rpc)
                args=rpc['params']['arguments'];self.assertNotIn('inventory_receipt',args)
                if rpc['params']['name']=='tabs' and args.get('action')=='list':
                    return reply(request,dict(isError=failed[0],content=[dict(type='text',text=json.dumps(page))]))
                if rpc['params']['name'] in ('windows','tab_groups'):
                    return reply(request,dict(content=[dict(type='text',text='[]')]))
                if rpc['params']['name']=='read':
                    return reply(request,dict(content=[dict(type='text',text='new user page body')]))
                return reply(request,dict(structuredContent=dict(ok=True,value=dict(
                    pages=[page],groups=[dict(groupId='user-group',tabIds=[101])],targets=[page['targetId']]))))
            bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=directory,task_id=TASK,
                client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            bridge.ensure_ready=AsyncMock()
            def request(i,name,**args):
                return dict(jsonrpc='2.0',id=i,method='tools/call',params=dict(name=name,arguments=args))
            async def call(i,name,**args):
                await bridge.handle(request(i,name,**args));return output[-1]['result']
            queued=None
            try:
                bridge.task_state.save('old-session',[],[])
                with bridge.task_state.browser_operation(exclusive=True) as guard:
                    expected=json.loads((Path(directory)/(TASK+'.json')).read_text())
                    bridge.task_state._write_record(closed(expected),administrator_reset=True,
                        expected_state=expected,operation_guard=guard)
                begun=await call(1,'workspace_cleanup',action='begin',task_id=TASK)
                self.assertTrue(begun['structuredContent']['freshInventoryRequired'])
                queued=bridge.handle(request(2,'tabs',action='close',page=1,session='old-session'))
                failed[0]=True  # Automatic catalogs must not authorize an old request on failure.
                for i,name,args in ((3,'read',dict(page=1)),(4,'tabs',dict(action='close',page=1)),
                        (5,'workspace_cleanup',dict(action='use',pageIds=[1],reason='old request',task_id=TASK))):
                    blocked=await call(i,name,**args)
                    self.assertTrue(blocked.get('isError') or blocked.get('structuredContent',{}).get('decision')=='block',output[-1])
                self.assertEqual(len(calls),3)
                self.assertTrue(all(c['params']['name']=='tabs' and c['params']['arguments']['action']=='list' for c in calls))
                self.assertEqual(bridge.ensure_ready.await_count,3)
                self.assertTrue(all(c.kwargs=={'allow_start':False} for c in bridge.ensure_ready.await_args_list))
                for i,name in ((6,'windows'),(7,'tab_groups')):
                    await call(i,name,action='list')
                    self.assertTrue(bridge.reset_requires_inventory)
                    self.assertIsNone(bridge.inventory_receipt)
                failed[0]=True
                bad=await call(8,'tabs',action='list')
                self.assertTrue(bad['isError']);self.assertNotIn(INVENTORY_META_KEY,bad.get('_meta',{}))
                self.assertTrue(bridge.reset_requires_inventory)
                failed[0]=False
                catalog=await call(9,'tabs',action='list')
                receipt=catalog['_meta'][INVENTORY_META_KEY]
                self.assertIn('new-user-target',catalog['content'][0]['text'])
                self.assertFalse(bridge.reset_requires_inventory)
                before=len(calls)
                await queued
                self.assertTrue(output[-1]['result']['isError']);self.assertEqual(len(calls),before)
                for i,args in ((10,dict(page=1)),(11,dict(page=1,inventory_receipt='wrong')),
                        (12,dict(page=1,session='old-session',inventory_receipt=receipt))):
                    self.assertTrue((await call(i,'read',**args))['isError'])
                    self.assertEqual(len(calls),before)
                original=request(13,'read',page=1,session='current-session',inventory_receipt=receipt)
                saved=copy.deepcopy(original)
                await bridge.handle(original)
                self.assertEqual(output[-1]['result']['content'][0]['text'],'new user page body')
                self.assertEqual(original,saved)
                self.assertFalse(any(c['params']['name']=='tabs' and c['params']['arguments'].get('action')=='close' for c in calls))
                self.assertEqual(page['ownership'],'user');self.assertIsNone(page['ownerAgentId'])
                # The receipt belongs to this live connection, not the durable
                # epoch or an unrelated/restarted client.
                cold=Bridge('http://127.0.0.1:9010/mcp',lambda _:None,state_root=directory,task_id=TASK)
                try:self.assertIsNone(cold.inventory_receipt);self.assertTrue(cold.reset_requires_inventory)
                finally:await cold.close()
            finally:
                if queued is not None:queued.close()
                await bridge.close()

    async def test_administrator_waits_for_inflight_post_and_internal_reconciliation(self):
        for phase in ('dispatch','reconciliation'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
                entered=asyncio.Event();release=asyncio.Event();admin_entered=asyncio.Event()
                item=dict(pageId=1,targetId='new-target',tabId=101,groupId='restored',url='https://example.com/a',ownership='user' if phase=='reconciliation' else 'mine',ownerAgentId=None)
                pages=[item] if phase=='reconciliation' else []
                groups=[dict(groupId='restored',title='task',tabIds=[101])] if pages else []
                actions=[]
                async def handler(request):
                    rpc=json.loads(request.content);name=rpc['params']['name'];args=rpc['params']['arguments']
                    if name=='tabs':
                        actions.append(args['action']);entered.set();await release.wait()
                        if phase=='dispatch':pages.append(item);groups.append(dict(groupId='restored',title='task',tabIds=[101]))
                        else:item['ownership']='mine'
                        return reply(request,dict(content=[dict(type='text',text='ack')]))
                    if args.get('code')=='return 1':return reply(request,dict(structuredContent=dict(ok=True,value=1)))
                    return reply(request,dict(structuredContent=dict(ok=True,value=dict(pages=pages,groups=groups,targets=[p['targetId'] for p in pages]))))
                bridge=Bridge('http://127.0.0.1:9010/mcp',lambda _:None,state_root=directory,task_id=TASK,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                    catalog={'catalog':{'tools':[dict(name='tabs',inputSchema=dict(properties=dict(action=dict(enum=['list','new','close','reclaim']))))]}})
                bridge.ensure_ready=AsyncMock();bridge.tool_session='current-session'
                old=dict(item,targetId='old-target',tabId=9,groupId='original',ownership='mine')
                bridge.task_state.save('old',[old] if phase=='reconciliation' else [],[dict(groupId='original',title='task',tabIds=[9])] if phase=='reconciliation' else [])
                admin=TaskState(directory);gate=admin.browser_operation(exclusive=True)
                async def administration():
                    async with gate:
                        admin_entered.set()
                        current=json.loads((Path(directory)/(TASK+'.json')).read_text(encoding='utf-8'))
                        self.assertEqual(current['pages'][0]['targetId'],'new-target')
                        # New live scope is observed after the in-flight result;
                        # offline closure verification would refuse a reset.
                request=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(
                    name='tabs' if phase=='dispatch' else 'run',arguments=dict(action='new',url='https://example.com/a') if phase=='dispatch' else dict(code='return 1')))
                work=asyncio.create_task(bridge.handle(request));manager=None
                try:
                    await asyncio.wait_for(entered.wait(),2)
                    manager=asyncio.create_task(administration())
                    for _ in range(100):
                        if gate.stream is not None:break
                        await asyncio.sleep(.005)
                    self.assertIsNotNone(gate.stream);self.assertFalse(admin_entered.is_set())
                    release.set();await asyncio.wait_for(work,3);await asyncio.wait_for(manager,3)
                    self.assertEqual(actions,['new' if phase=='dispatch' else 'reclaim'])
                finally:
                    release.set()
                    if not work.done():work.cancel()
                    if manager and not manager.done():manager.cancel()
                    await asyncio.gather(work,*([manager] if manager else []),return_exceptions=True)
                    await bridge.close()

    async def test_queued_prior_request_requires_fresh_inventory_after_reset(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            calls=[];output=[]
            def forbidden(request):calls.append(request);raise AssertionError('Prior request must not dispatch')
            bridge=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=directory,task_id=TASK,
                client=httpx.AsyncClient(transport=httpx.MockTransport(forbidden)))
            bridge.ensure_ready=AsyncMock();bridge.tool_session='old-session';bridge.task_state.save('old-session',[],[])
            work=None
            try:
                async with bridge.task_state.browser_operation(exclusive=True) as admin:
                    work=asyncio.create_task(bridge.handle(dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='tabs',arguments=dict(action='new',url='about:blank',session='old-session')))))
                    await asyncio.sleep(.03)
                    self.assertFalse(work.done());self.assertEqual(calls,[])
                    path=Path(directory)/(TASK+'.json');observed=json.loads(path.read_text(encoding='utf-8'))
                    bridge.task_state._write_record(closed(observed),administrator_reset=True,expected_state=copy.deepcopy(observed),operation_guard=admin)
                await asyncio.wait_for(work,2)
                self.assertTrue(output[-1]['result']['isError']);self.assertIn('tabs action=list',json.dumps(output[-1]))
                self.assertEqual(calls,[]);bridge.ensure_ready.assert_not_awaited()
                await bridge.handle(dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='workspace_cleanup',arguments=dict(action='begin',task_id=TASK))))
                self.assertTrue(output[-1]['result']['structuredContent']['browserReset'])
                self.assertFalse(bridge.reset_requires_begin)
            finally:
                if work and not work.done():work.cancel();await asyncio.gather(work,return_exceptions=True)
                await bridge.close()

    async def test_cancelling_a_waiter_closes_its_handle(self):
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as directory:
            gate=Path(directory)/'boundary';waiter=OperationBoundary(gate)
            async def wait():
                async with waiter:self.fail('Waiter crossed exclusive boundary')
            async with OperationBoundary(gate,exclusive=True):
                task=asyncio.create_task(wait())
                for _ in range(100):
                    if waiter.stream is not None:break
                    await asyncio.sleep(.005)
                self.assertIsNotNone(waiter.stream)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
                self.assertIsNone(waiter.stream)
            async with OperationBoundary(gate,exclusive=True):pass
