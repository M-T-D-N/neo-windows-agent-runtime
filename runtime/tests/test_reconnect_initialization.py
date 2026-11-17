"""Reconnect initialization is shared by every task and client, never action replay."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge, SESSION_META_KEY, INVENTORY_META_KEY

TASK = '00000000-0000-4000-8000-000000000001'
RESET = '00000000-0000-4000-8000-000000000002'


def call(number, name, **arguments):
    return dict(jsonrpc='2.0', id=number, method='tools/call',
                params=dict(name=name, arguments=arguments))


class ReconnectInitializationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.fail_inventory = False

    def bridge(self, client='codex', task=TASK, *, bound=True, pending=False):
        root = Path(self.temp.name) / client
        root.mkdir(exist_ok=True)
        record = dict(schema_version=1, task_id=task, session=None if pending else 'current',
                      pages_session=None if pending else 'current', pages=[], groups=[], used_tabs=[],
                      retained=[], prior_bindings=[], review_required=[], browser_reset_id=RESET,
                      browser_reset_verified=True, browser_reset_pending=pending, browser_closed=pending)
        (root / (task + '.json')).write_text(json.dumps(record), encoding='utf-8')
        def handler(request):
            rpc = json.loads(request.content)
            self.calls.append(rpc)
            self.assertEqual(rpc['params']['name'], 'tabs')
            self.assertEqual(rpc['params']['arguments']['action'], 'list')
            self.assertNotIn('page', rpc['params']['arguments'])
            self.assertNotIn('inventory_receipt', rpc['params']['arguments'])
            return httpx.Response(200, json=dict(jsonrpc='2.0', id=rpc['id'], result=dict(
                isError=self.fail_inventory, content=[dict(type='text', text='[7] about:blank (current tab)')],
                _meta={SESSION_META_KEY: 'current'})))
        output = []
        bridge = Bridge('http://127.0.0.1:9010/mcp', output.append, state_root=root,
                        task_id=task if bound else None,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        bridge.ensure_ready = AsyncMock()
        self.addAsyncCleanup(bridge.close)
        return bridge, output

    async def test_two_tasks_and_both_clients_list_without_redundant_begin(self):
        for client in ('codex', 'agy'):
            for task in (TASK, '00000000-0000-4000-8000-000000000003'):
                with self.subTest(client=client, task=task):
                    before = len(self.calls)
                    bridge, output = self.bridge(client, task)
                    self.assertFalse(bridge.reset_requires_begin)
                    self.assertEqual(len(self.calls), before)
                    await bridge.handle(call(1, 'tabs', action='list'))
                    self.assertFalse(output[-1]['result'].get('isError'))
                    self.assertIn(INVENTORY_META_KEY, output[-1]['result']['_meta'])
                    self.assertFalse(bridge.reset_requires_inventory)

    async def test_stale_page_request_delivers_catalog_only_then_queued_request_stays_blocked(self):
        bridge, output = self.bridge()
        original = call(1, 'tabs', action='close', page=7, session='retired')
        saved = copy.deepcopy(original)
        queued = bridge.handle(call(2, 'run', code='dangerous old action'))
        await bridge.handle(original)
        initialized = output[-1]['result']
        self.assertTrue(initialized['isError'])
        self.assertFalse(initialized['structuredContent']['requestedActionExecuted'])
        self.assertTrue(initialized['structuredContent']['requiresFreshRequest'])
        self.assertIn('[7] about:blank', initialized['content'][1]['text'])
        self.assertEqual(original, saved)
        self.assertEqual(len(self.calls), 1)
        await queued
        self.assertTrue(output[-1]['result']['isError'])
        self.assertEqual(len(self.calls), 1)
        for receipt in (None, 'wrong'):
            await bridge.handle(call(3, 'tabs', action='close', page=7, inventory_receipt=receipt))
            self.assertTrue(output[-1]['result']['isError'])
            self.assertEqual(len(self.calls), 1)

    async def test_first_cleanup_use_initializes_bound_task_without_start_or_using_old_id(self):
        bridge, output = self.bridge('agy', bound=False)
        await bridge.handle(call(1, 'workspace_cleanup', action='use', task_id=TASK,
                                 pageIds=[99], reason='stale original request'))
        value = output[-1]['result']['structuredContent']
        self.assertTrue(value['taskBound'])
        self.assertTrue(value['freshInventoryProvided'])
        self.assertFalse(value['requestedActionExecuted'])
        self.assertEqual(bridge.cleanup.used, {})
        bridge.ensure_ready.assert_awaited_once_with(allow_start=False)
        self.assertFalse(bridge.reset_requires_inventory)

    async def test_first_resume_returns_inventory_without_adopting_groups(self):
        bridge, output = self.bridge('agy', pending=True)
        bridge.prepare_task = AsyncMock(side_effect=AssertionError('Do not adopt during initialization'))
        await bridge.handle(call(1, 'workspace_cleanup', action='resume', task_id=TASK))
        self.assertTrue(output[-1]['result']['structuredContent']['initializationOnly'])
        bridge.ensure_ready.assert_awaited_once_with(allow_start=False)
        bridge.prepare_task.assert_not_awaited()
        self.assertEqual(bridge.task_state.record['pages'], [])

    async def test_catalog_discovery_and_closed_finish_check_never_contact_or_start_browser(self):
        bridge, output = self.bridge(pending=True)
        await bridge.handle(dict(jsonrpc='2.0', id=1, method='tools/list'))
        await bridge.handle(call(2, 'workspace_cleanup', action='check', task_id=TASK))
        self.assertFalse(output[-1]['result']['structuredContent']['browserUsed'])
        self.assertEqual(self.calls, [])
        bridge.ensure_ready.assert_not_awaited()

    async def test_failed_inventory_and_delivery_keep_page_gate_closed(self):
        bridge, output = self.bridge()
        self.fail_inventory = True
        await bridge.handle(call(1, 'read', page=7))
        self.assertTrue(output[-1]['result']['isError'])
        self.assertTrue(bridge.reset_requires_inventory)
        self.assertIsNone(bridge.inventory_receipt)
        self.fail_inventory = False
        bridge.emit = lambda _: (_ for _ in ()).throw(RuntimeError('synthetic delivery failure'))
        with self.assertRaises(RuntimeError):
            await bridge.handle(call(2, 'read', page=7))
        self.assertTrue(bridge.reset_requires_inventory)

    async def test_task_identity_is_not_guessed_or_switched(self):
        bridge, output = self.bridge(bound=False)
        await bridge.handle(call(1, 'read', page=7))
        self.assertTrue(output[-1]['result']['isError'])
        self.assertEqual(self.calls, [])

        await bridge.handle(call(2, 'workspace_cleanup', action='begin', task_id=TASK))
        await bridge.handle(call(3, 'workspace_cleanup', action='resume',
                                 task_id='00000000-0000-4000-8000-000000000003'))
        self.assertEqual(output[-1]['result']['structuredContent']['decision'], 'block')
        self.assertEqual(self.calls, [])

    async def test_offline_bootstrap_never_invokes_starter_or_original_action(self):
        bridge, output = self.bridge()
        await bridge.client.aclose()
        def refused(request):
            rpc = json.loads(request.content)
            self.assertIn(rpc['method'], ('ping', 'initialize'))
            raise httpx.ConnectError('offline fixture', request=request)
        bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(refused))
        bridge.ensure_ready = Bridge.ensure_ready.__get__(bridge)
        bridge.initialize = dict(jsonrpc='2.0', id=0, method='initialize', params=dict(protocolVersion='2026-07-28'))
        bridge.starter = AsyncMock(side_effect=AssertionError('Do not reopen Neo'))
        await bridge.handle(call(1, 'tabs', action='close', page=7))
        self.assertTrue(output[-1]['result']['isError'])
        self.assertIn('never starts', output[-1]['result']['content'][0]['text'])
        self.assertTrue(bridge.reset_requires_inventory)
        bridge.starter.assert_not_awaited()
