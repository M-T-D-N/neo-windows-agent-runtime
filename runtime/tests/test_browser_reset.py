"""A verified administrator close-all retires cached handles without browser I/O."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge

TASK = '00000000-0000-4000-8000-000000000001'
RESET = '00000000-0000-4000-8000-000000000002'


class BrowserResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.calls = []
        def forbidden(request):
            self.calls.append(request)
            raise AssertionError('A local close-all receipt must not contact Neo')
        self.output = []
        self.bridge = Bridge('http://127.0.0.1:9010/mcp', self.output.append,
            state_root=self.temp.name, task_id=TASK,
            client=httpx.AsyncClient(transport=httpx.MockTransport(forbidden)))
        self.old = dict(self.bridge.task_state.record, session='old-session', pages=[{'targetId':'old'}],
            groups=[{'groupId':'old-group'}], used_tabs=[{'targetId':'used'}],
            retained=[[['old-session','old'],'needed']], outputs=[{'path':'preserved-output'}])
        self.bridge.task_state.record = copy.deepcopy(self.old)
        self.bridge.tool_session = 'old-session'
        self.bridge.task_prepared = True
        self.bridge.cleanup.used = {'used':{'targetId':'used'}}
        self.bridge.cleanup.retained = {('old-session','old'):'needed'}
        self.bridge.cleanup.review_required = {('old-session','old')}
        self.path = Path(self.temp.name)/(TASK+'.json')

    async def asyncTearDown(self):
        await self.bridge.close()
        self.temp.cleanup()

    def write_receipt(self, **overrides):
        record = dict(self.old, browser_reset_id=RESET, browser_reset_verified=True, browser_reset_pending=True, browser_closed=True, session=None,
            pages=[], groups=[], used_tabs=[], retained=[], prior_bindings=[], review_required=[])
        record.update(overrides)
        # Deliberately inject administrator/invalid disk fixtures; production
        # administration uses _write_record(administrator_reset=True).
        from workspace_storage import atomic_write_json
        atomic_write_json(self.path, record, backup_count=0)
        return record

    def admin_write(self,state,receipt,expected):
        with state.browser_operation(exclusive=True) as guard:
            state._write_record(receipt,administrator_reset=True,expected_state=expected,operation_guard=guard)

    async def test_begin_refreshes_cached_closed_scope_without_starting_browser(self):
        fresh = self.write_receipt()
        await self.bridge.handle(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(
            name='workspace_cleanup', arguments=dict(action='begin', task_id=TASK))))
        result = self.output[-1]['result']['structuredContent']
        self.assertTrue(result['browserReset'])
        self.assertFalse(result['browserStarted'])
        self.assertEqual(result['priorPages'], [])
        self.assertEqual(result['priorUsedTabs'], [])
        self.assertEqual(self.bridge.task_state.record, fresh)
        self.assertIsNone(self.bridge.tool_session)
        self.assertFalse(self.bridge.task_prepared)
        self.assertEqual(self.bridge.cleanup.used, {})
        self.assertEqual(self.bridge.cleanup.retained, {})
        self.assertEqual(self.bridge.cleanup.review_required, set())
        self.assertEqual(self.bridge.task_state.record['outputs'], self.old['outputs'])
        self.assertEqual(self.calls, [])

    async def test_first_browser_request_retires_stale_hint_without_mutating_input(self):
        self.write_receipt()
        message = dict(jsonrpc='2.0', id=2, method='tools/call', params=dict(
            name='run', arguments=dict(session='old-session', code='return 1')))
        original = copy.deepcopy(message)
        refreshed = self.bridge.refresh_reset_binding(message)
        self.assertNotIn('session', refreshed['params']['arguments'])
        self.assertEqual(message, original)
        self.assertEqual(self.calls, [])

    async def test_incomplete_or_foreign_receipt_preserves_cache(self):
        for invalid in ({'pages':[{'targetId':'live'}]}, {'browser_closed':False},
            {'session':'live'}, {'pages_session':'live'}, {'task_id':RESET}, {'schema_version':2},
                        {'browser_reset_id':'invalid'}, {'used_tabs':None}):
            with self.subTest(invalid=invalid):
                self.write_receipt(**invalid)
                with self.assertRaises(ValueError):
                    self.bridge.bind_task(TASK)
                self.assertEqual(self.bridge.task_state.record, self.old)
                self.assertEqual(self.bridge.tool_session, 'old-session')
        self.assertEqual(self.calls, [])

    async def test_ordinary_concurrent_write_does_not_replace_cache(self):
        self.path.write_text(json.dumps(dict(self.old, pages=[])), encoding='utf-8')
        self.bridge.bind_task(TASK)
        self.assertEqual(self.bridge.task_state.record, self.old)
        self.assertEqual(self.calls, [])

    async def test_new_browser_activity_keeps_new_handle_after_consuming_reset(self):
        self.write_receipt()
        self.bridge.bind_task(TASK)
        self.bridge.tool_session = 'new-session'
        self.bridge.consume_reset_receipt()
        message = dict(params=dict(arguments=dict(session='new-session', code='return 1')))
        self.assertIs(self.bridge.refresh_reset_binding(message), message)
        self.assertEqual(self.bridge.tool_session, 'new-session')
        self.assertEqual(self.calls, [])

        # A later ordinary close may keep its real session without looking
        # like another unconsumed administrator receipt on a cold connection.
        self.bridge.task_state.record['browser_closed'] = True
        self.bridge.task_state._write_record()
        from neo_task_state import TaskState
        cold = TaskState(self.temp.name)
        self.assertEqual(cold.bind(TASK)['session'], 'new-session')

    async def test_readiness_failure_cannot_reintroduce_retired_hint(self):
        from unittest.mock import AsyncMock
        from neo_mcp_bridge import UpstreamError
        self.write_receipt()
        await self.bridge.handle(dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(
            name='workspace_cleanup',arguments=dict(action='begin',task_id=TASK))))
        self.bridge.ensure_ready = AsyncMock(side_effect=UpstreamError('synthetic readiness failure'))
        for rpc_id in (3, 4):
            await self.bridge.handle(dict(jsonrpc='2.0', id=rpc_id, method='tools/call', params=dict(
                name='tabs', arguments=dict(session='old-session', action='list'))))
            self.assertTrue(self.output[-1]['result']['isError'])
            self.assertIsNone(self.bridge.tool_session)
            self.assertTrue(self.bridge.reset_pending)
        self.assertEqual(self.bridge.ensure_ready.await_count,2)
        self.assertEqual(self.calls, [])

    async def test_cold_load_rejects_incomplete_closed_receipt(self):
        from neo_task_state import TaskState
        self.write_receipt(session='old-session')
        cold = TaskState(self.temp.name)
        with self.assertRaises(ValueError):
            cold.bind(TASK)
        self.assertIsNone(cold.record)

    async def test_consumed_receipt_still_rejects_old_hint(self):
        self.write_receipt()
        self.bridge.bind_task(TASK)
        self.bridge.tool_session = 'new-session'
        self.bridge.consume_reset_receipt()
        message = dict(params=dict(arguments=dict(session='old-session', code='return 1')))
        self.assertNotIn('session', self.bridge.refresh_reset_binding(message)['params']['arguments'])
        self.assertEqual(self.bridge.tool_session, 'new-session')
        self.assertEqual(self.calls, [])

    async def test_two_cached_connections_can_observe_receipt_before_or_after_consumption(self):
        for before in (True, False):
            with self.subTest(before=before):
                self.bridge.task_state.record = copy.deepcopy(self.old)
                self.bridge.tool_session = 'old-session'
                other = Bridge('http://127.0.0.1:9010/mcp', lambda _:None, state_root=self.temp.name,
                    client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:self.fail('No HTTP'))))
                other.task_state.task_id = TASK
                other.task_state.record = copy.deepcopy(self.old)
                other.tool_session = 'old-session'
                try:
                    self.write_receipt()
                    if before:other.bind_task(TASK)
                    self.bridge.bind_task(TASK)
                    self.bridge.tool_session = 'new-session'
                    self.bridge.consume_reset_receipt()
                    other.bind_task(TASK)
                    self.assertEqual(other.tool_session, 'new-session')
                    self.assertFalse(other.reset_pending)
                    self.assertEqual(other.cleanup.used, {})
                    self.assertEqual(other.task_state.record['outputs'],self.old['outputs'])
                finally:
                    await other.close()

    async def test_missing_receipt_outputs_preserve_cached_provenance(self):
        self.write_receipt(outputs=[])
        self.bridge.bind_task(TASK)
        self.assertEqual(self.bridge.task_state.record['outputs'],self.old['outputs'])

    async def test_conflicting_receipt_outputs_fail_without_replacing_cache(self):
        self.write_receipt(outputs=[{'path':'preserved-output','changed':True}])
        with self.assertRaisesRegex(ValueError,'Conflicting output'):
            self.bridge.bind_task(TASK)
        self.assertEqual(self.bridge.task_state.record,self.old)

    async def test_administrator_reset_merges_latest_outputs_and_rejects_stale_writer(self):
        from workspace_storage import atomic_write_json
        atomic_write_json(self.path,dict(self.old, outputs=self.old['outputs']+[{'path':'concurrent-output'}]),backup_count=0)
        expected=json.loads(self.path.read_text(encoding='utf-8'))
        receipt = dict(self.old, browser_reset_id=RESET, browser_reset_verified=True,browser_reset_pending=True,
            browser_closed=True,session=None,pages_session=None,pages=[],groups=[],used_tabs=[],retained=[],
            prior_bindings=[],review_required=[],outputs=[])
        self.admin_write(self.bridge.task_state,receipt,expected)
        disk=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual({e['path'] for e in disk['outputs']},{'preserved-output','concurrent-output'})
        with self.assertRaisesRegex(ValueError,'Reset changed'):
            self.bridge.task_state._write_record(self.old)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),disk)

    async def test_two_concurrent_output_updates_are_merged(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from neo_task_state import TaskState
        self.bridge.task_state._write_record(self.old)
        a,b=TaskState(self.temp.name),TaskState(self.temp.name)
        a.bind(TASK);b.bind(TASK)
        barrier=threading.Barrier(2)
        def append(state,name):
            updated=dict(state.record,outputs=state.record['outputs']+[{'path':name}])
            barrier.wait(timeout=5)
            state._write_record(updated)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(append,s,n) for s,n in ((a,'a-output'),(b,'b-output'))]
            for future in futures:future.result(timeout=10)
        disk=json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual({e['path'] for e in disk['outputs']},{'preserved-output','a-output','b-output'})

    async def test_two_inflight_reset_mints_cannot_replace_first_consumed_handle(self):
        from neo_mcp_bridge import UpstreamError
        other = Bridge('http://127.0.0.1:9010/mcp', lambda _:None, state_root=self.temp.name,
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _:self.fail('No HTTP'))))
        other.task_state.task_id=TASK;other.task_state.record=copy.deepcopy(self.old)
        try:
            self.write_receipt()
            self.bridge.bind_task(TASK);other.bind_task(TASK)
            self.bridge.tool_session='first-new-session';self.bridge.consume_reset_receipt()
            # B's earlier in-flight inventory may return a different minted
            # handle after A completed the same reset. No browser mutation may
            # follow that stale preflight or overwrite A's persisted scope.
            other.tool_session='second-new-session'
            with self.assertRaisesRegex(UpstreamError,'another connection'):
                other.consume_reset_receipt()
            disk=json.loads(self.path.read_text(encoding='utf-8'))
            self.assertEqual(disk['session'],'first-new-session')
            self.assertEqual(other.tool_session,'first-new-session')
            self.assertFalse(other.reset_pending)
        finally:
            await other.close()

    async def test_delayed_administrator_cannot_replace_new_session_or_binding(self):
        state=self.bridge.task_state
        state._write_record(self.old)
        expected=copy.deepcopy(json.loads(self.path.read_text(encoding='utf-8')))
        receipt=dict(self.old,browser_reset_id=RESET,browser_reset_verified=True,browser_reset_pending=True,
            browser_closed=True,session=None,pages_session=None,pages=[],groups=[],used_tabs=[],retained=[],
            prior_bindings=[],review_required=[])
        self.admin_write(state,receipt,expected)
        self.bridge.bind_task(TASK)
        self.bridge.tool_session='new-session';self.bridge.consume_reset_receipt()
        state.save('new-session',[dict(targetId='new-page',tabId=404,groupId='new-group',
            url='https://example.com/new',ownership='mine')],[dict(groupId='new-group',tabIds=[404])])
        before=self.path.read_bytes()
        late=dict(receipt,browser_reset_id='00000000-0000-4000-8000-000000000003')
        with self.assertRaisesRegex(ValueError,'changed since closure'):
            self.admin_write(state,late,expected)
        self.assertEqual(self.path.read_bytes(),before)
        fresh=copy.deepcopy(json.loads(before))
        with self.assertRaisesRegex(ValueError,'already issued'):
            self.admin_write(state,receipt,fresh)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(state.record['session'],'new-session')
        self.assertEqual(state.record['pages'][0]['targetId'],'new-page')

    async def test_administrator_requires_snapshot_and_detects_activity_returning_to_same_scope(self):
        state=self.bridge.task_state;state._write_record(self.old)
        expected=copy.deepcopy(json.loads(self.path.read_text(encoding='utf-8')))
        receipt=dict(self.old,browser_reset_id=RESET,browser_reset_verified=True,browser_reset_pending=True,
            browser_closed=True,session=None,pages_session=None,pages=[],groups=[],used_tabs=[],retained=[],
            prior_bindings=[],review_required=[])
        before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'changed since closure'):
            self.admin_write(state,receipt,None)
        self.assertEqual(self.path.read_bytes(),before)
        state._write_record()  # mutation-capable dispatch advances revision
        after=self.path.read_bytes()
        self.assertNotEqual(json.loads(after)['state_revision'],expected['state_revision'])
        with self.assertRaisesRegex(ValueError,'changed since closure'):
            self.admin_write(state,receipt,expected)
        self.assertEqual(self.path.read_bytes(),after)
