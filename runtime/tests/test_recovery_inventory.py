"""The supported metadata read must not depend on successful ownership recovery."""
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
from neo_mcp_bridge import Bridge, SESSION_META_KEY
from neo_cleanup import TOOL

TASK = '00000000-0000-4000-8000-000000000001'
INIT = {'params': {'protocolVersion': '2026-07-28', 'capabilities': {},
                   'clientInfo': {'name': 'codex-test', 'version': '1'}}}
MEMBER = dict(pageId=10, tabId=20, targetId='current-target', groupId='current-group',
              windowId=30, url='https://example.com/login', ownership='user', ownerAgentId=None,
              title='not part of metadata contract', body='private draft must not escape')


class RecoveryInventoryTests(unittest.IsolatedAsyncioTestCase):
    def setup_bridge(self, pages=None, groups=None, failure=None):
        temporary = tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(temporary.cleanup)
        self.calls = []
        self.pages = [copy.deepcopy(MEMBER)] if pages is None else pages
        self.groups = [dict(id='current-group', title='private title')] if groups is None else groups
        self.output = []
        def handler(request):
            rpc = json.loads(request.content)
            self.calls.append(rpc)
            if failure:
                raise httpx.ConnectError('endpoint offline', request=request)
            result = {'_meta': {SESSION_META_KEY: 'fresh-session'},
                      'structuredContent': {'ok': True, 'value': {'pages': self.pages, 'groups': self.groups}}}
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': rpc['id'], 'result': result})
        bridge = Bridge('http://127.0.0.1:9010/mcp', self.output.append,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        state_root=temporary.name, task_id=TASK)
        self.addAsyncCleanup(bridge.close)
        bridge.initialize = INIT
        bridge.starter = AsyncMock(side_effect=AssertionError('inventory must not start Neo'))
        bridge.ensure_ready = AsyncMock(side_effect=AssertionError('inventory must not run readiness startup'))
        bridge.task_state.save('old-session', [dict(targetId='old-target', groupId='old-group',
            url='https://example.com/offer', ownership='mine')], [dict(id='old-group', title='task')],
            retained=[(('old-session', 'old-target', 'https://example.com/offer'), 'Preserve pending draft')])
        bridge.task_state.record.update(used_tabs=[dict(targetId='old-target', purpose='follow-up')],
                                      execution_id='prior', execution_incomplete=True, browser_closed=False)
        bridge.task_state._write_record()
        bridge.tool_session = 'old-session'
        self.path = Path(temporary.name) / (TASK + '.json')
        self.before = self.path.read_bytes()
        return bridge

    async def call(self, bridge, **args):
        await bridge.handle({'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'workspace_cleanup', 'arguments': dict(action='inventory', task_id=TASK, **args)}})
        return self.output[-1]['result']['structuredContent']

    async def test_conflict_metadata_is_scoped_deduplicated_and_never_claims_or_saves(self):
        other = dict(MEMBER, pageId=11, ownership='other-agent', ownerAgentId='active-other')
        unrelated = dict(MEMBER, pageId=12, targetId='unrelated', groupId='unrelated', url='https://example.com/other')
        bridge = self.setup_bridge(pages=[copy.deepcopy(MEMBER), other, unrelated])
        bridge.prepare_task = AsyncMock(side_effect=AssertionError('inventory must skip reconciliation'))
        value = await self.call(bridge, group_id='current-group')
        self.assertTrue(value['inventoryOnly'])
        self.assertEqual(value['recoveryPages'], [dict(targetId='current-target', url=MEMBER['url'])])
        self.assertEqual([p['pageId'] for p in value['pages']], [10, 11])
        self.assertEqual(value['pages'][1]['ownership'], 'other-agent')
        self.assertNotIn('private', json.dumps(value))
        self.assertNotIn('title', json.dumps(value))
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(bridge.task_prepared)
        bridge.prepare_task.assert_not_awaited()
        bridge.ensure_ready.assert_not_awaited()
        bridge.starter.assert_not_awaited()
        self.assertEqual(self.output[-1]['result']['_meta'][SESSION_META_KEY], 'fresh-session')
        self.assertEqual([(c['params']['name'], c['params']['arguments']['code']) for c in self.calls],
                         [('run', 'const pages=await browser.pages.list(); const targets=(await browser.cdp("Target.getTargets",{})).targetInfos.map(t=>t.targetId); const groups=(await browser.cdp("Browser.getTabGroups",{})).groups; return {pages:pages.filter(p=>!p.targetId || targets.includes(p.targetId)),groups};')])
        self.assertEqual(bridge.task_state.record['pages'][0]['targetId'], 'old-target')

    async def test_invalid_scope_or_extra_arguments_never_dispatch(self):
        bridge = self.setup_bridge()
        for args in ({}, {'group_id': ''}, {'group_id': '  '}, {'group_id': 3},
                     {'group_id': 'current-group', 'pageIds': [10]},
                     {'group_id': 'current-group', 'code': 'arbitrary JS'},
                     {'group_id': 'current-group', 'session': 'untrusted'},
                     {'group_id': 'current-group', 'recovery': {}}):
            with self.subTest(args=args):
                result = await self.call(bridge, **args)
                self.assertEqual(result['decision'], 'block')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertIn('inventory', TOOL['inputSchema']['properties']['action']['enum'])
        self.assertIn('group_id', TOOL['inputSchema']['properties'])

    async def test_absent_incomplete_conflicting_or_sensitive_metadata_fails_without_record_changes(self):
        for pages, groups in (([], []), ([dict(MEMBER, targetId=None)], [dict(id='current-group')]),
                ([dict(MEMBER, url=None)], [dict(id='current-group')]),
                ([dict(MEMBER, url='https://example.com/?token=secret')], [dict(id='current-group')]),
                ([copy.deepcopy(MEMBER)], [dict(id='current-group'), dict(id='current-group')]),
                ([copy.deepcopy(MEMBER), dict(MEMBER, url='https://example.com/changed')], [dict(id='current-group')])):
            with self.subTest(pages=pages, groups=groups):
                bridge = self.setup_bridge(pages=pages, groups=groups)
                value = await self.call(bridge, group_id='current-group')
                self.assertEqual(value['decision'], 'block')
                self.assertNotIn('secret', json.dumps(value))
                self.assertEqual(self.path.read_bytes(), self.before)
                bridge.starter.assert_not_awaited()

    async def test_unreachable_browser_is_not_started_or_reported_as_empty_inventory(self):
        bridge = self.setup_bridge(failure=True)
        value = await self.call(bridge, group_id='current-group')
        self.assertEqual(value['decision'], 'block')
        self.assertNotIn('pages', value)
        self.assertEqual(self.path.read_bytes(), self.before)
        bridge.starter.assert_not_awaited()
        bridge.ensure_ready.assert_not_awaited()

    async def test_metadata_does_not_authorize_changed_membership_or_another_owner_on_resume(self):
        for change in ('other-owner', 'extra-member', 'changed-url'):
            with self.subTest(change=change):
                bridge = self.setup_bridge()
                value = await self.call(bridge, group_id='current-group')
                if change == 'other-owner':
                    self.pages.append(dict(MEMBER, ownership='other-agent', ownerAgentId='active-other'))
                elif change == 'extra-member':
                    self.pages.append(dict(MEMBER, targetId='new-member', pageId=12))
                else:
                    self.pages[0]['url'] = 'https://example.com/changed'
                bridge.ensure_ready = AsyncMock()
                before = json.loads(self.before)
                await bridge.handle({'jsonrpc': '2.0', 'id': 10, 'method': 'tools/call',
                    'params': {'name': 'workspace_cleanup', 'arguments': dict(action='resume', task_id=TASK,
                        recovery=dict(prior_group_id='old-group', group_id='current-group',
                            pages=value['recoveryPages'], reason='User selected the exact synthetic recovery group'))}})
                result = self.output[-1]['result']['structuredContent']
                self.assertEqual(result['decision'], 'block')
                after = json.loads(self.path.read_bytes())
                for key in ('pages', 'groups', 'retained', 'used_tabs'):
                    self.assertEqual(after[key], before[key])
                self.assertTrue(all(c['params']['name'] == 'run' for c in self.calls))
                bridge.starter.assert_not_awaited()
