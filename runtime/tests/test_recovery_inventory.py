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
from neo_task_state import TaskState

TASK = '00000000-0000-4000-8000-000000000001'
INIT = {'params': {'protocolVersion': '2026-07-28', 'capabilities': {},
                   'clientInfo': {'name': 'codex-test', 'version': '1'}}}
MEMBER = dict(pageId=10, tabId=20, targetId='current-target', groupId='current-group',
              windowId=30, url='https://example.com/login', ownership='user', ownerAgentId=None,
              title='not part of metadata contract', body='private draft must not escape')


class RetireMissingGroupTests(unittest.IsolatedAsyncioTestCase):
    def setup_task(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / (TASK + '.json')
        prior = dict(targetId='old-target', tabId=21, groupId='old-group',
                     url='https://example.com/previous')
        sibling = dict(targetId='sibling-target', tabId=22, groupId='sibling-group',
                       url='https://example.com/sibling')
        saved = dict(schema_version=1, task_id=TASK, state_revision=73, session='same-session',
                     pages_session='older-session', pages=[prior, sibling], groups=[
                         dict(id='old-group'), dict(id='sibling-group')],
                     prior_bindings=[dict(prior, session='older-session')],
                     used_tabs=[dict(targetId=prior['targetId']), dict(targetId='other-user')],
                     retained=[[['older-session', prior['targetId'], prior['url']], 'Old work']],
                     review_required=[['older-session', prior['targetId'], prior['url']]],
                     outputs=[dict(path='old-result.txt')])
        self.path.write_text(json.dumps(saved), encoding='utf-8')
        self.saved = saved
        self.neighbor = self.root / 'unrelated.json'
        self.neighbor.write_text('unrelated untouched', encoding='utf-8')
        self.pages = [dict(sibling, pageId=22, ownership='mine'),
                      dict(targetId='other-user', tabId=23, pageId=23, groupId=None,
                           url='https://example.com/other', ownership='user')]
        for page in self.pages:
            page['windowId'] = 30
        self.groups = [dict(id='sibling-group')]
        self.native_only = []
        self.change_revision_during_proof = False
        self.disagree = False
        self.inventory_reads = 0
        self.inventory_transform = None
        self.calls = []
        self.selection = dict(group_id='old-group', revision=73,
                              target_ids=['old-target'], confirmed=True)

    def connect(self):
        output = []
        def handler(request):
            rpc = json.loads(request.content)
            params = rpc.get('params') or {}
            name = params.get('name')
            args = params.get('arguments') or {}
            self.calls.append((name or rpc['method'], args))
            result = {'_meta': {SESSION_META_KEY: 'same-session'}}
            if name == 'run':
                tabs = [{k:p[k] for k in ('targetId','tabId','url','groupId','windowId')} for p in self.pages]
                targets = [dict(targetId=p['targetId'], url=p['url'], type='page') for p in tabs]
                targets.extend(self.native_only)
                if 'Browser.getTabs' in args['code']:
                    self.inventory_reads += 1
                    if self.change_revision_during_proof:
                        self.path.write_text(json.dumps(dict(self.saved, state_revision=74)), encoding='utf-8')
                    groups = [dict(group, windowId=30,
                                   tabIds=[t['tabId'] for t in tabs if t.get('groupId') == group['id']])
                              for group in self.groups]
                    windows = [dict(windowId=30, tabCount=len(tabs))]
                    data = dict(tabs=tabs, pages=self.pages, groups=groups, targets=targets, windows=windows)
                    if self.disagree:
                        data['targets'] = []
                    if self.inventory_transform:
                        self.inventory_transform(self.inventory_reads, data)
                else:
                    data = dict(pages=self.pages, groups=self.groups,
                                targets=[t['targetId'] for t in targets])
                result['structuredContent'] = {'ok': True, 'value': data}
            if name == 'tabs':
                if args['action'] == 'new':
                    self.pages.append(dict(targetId='fresh-target', pageId=99, tabId=99,
                                           url=args['url'], groupId=None, windowId=30, ownership='mine'))
                if args['action'] == 'close':
                    self.pages = [page for page in self.pages if page['pageId'] != args['page']]
                result['content'] = [{'type':'text','text':'native tabs response'}]
            return httpx.Response(200, json={'jsonrpc':'2.0', 'id':rpc['id'], 'result':result})
        bridge = Bridge('http://127.0.0.1:9010/mcp', output.append,
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        state_root=self.root, task_id=TASK)
        bridge.initialize = INIT
        bridge.starter = AsyncMock(side_effect=AssertionError('Retirement has no launch authority'))
        self.addAsyncCleanup(bridge.close)
        return bridge, output

    def cleanup_request(self, action='retire', **args):
        return {'jsonrpc':'2.0', 'id':1, 'method':'tools/call', 'params':{
            'name':'workspace_cleanup', 'arguments':dict(action=action, task_id=TASK, **args)}}

    def tabs_request(self, action, **args):
        return {'jsonrpc':'2.0', 'id':2, 'method':'tools/call', 'params':{
            'name':'tabs', 'arguments':dict(action=action, **args)}}

    async def test_explicit_retirement_drops_only_the_absent_group_and_reloads_stale_bridge(self):
        self.setup_task()
        bridge, output = self.connect()
        stale, stale_output = self.connect()
        await bridge.handle(self.cleanup_request('begin'))
        begin = output[-1]['result']['structuredContent']
        self.assertEqual(begin['stateRevision'], 73)
        self.assertEqual(begin['recordedGroupTargets']['old-group'], ['old-target'])
        await bridge.handle(self.cleanup_request('resume'))
        self.assertIn('read-only restored candidates', json.dumps(output[-1]))
        reason = output[-1]['result']['structuredContent']['reason']
        self.assertEqual(json.loads(reason.rsplit('candidates: ', 1)[1]),
                         [{'priorGroupId': 'old-group', 'candidates': []}])
        self.assertEqual(json.loads(self.path.read_bytes()), self.saved)
        self.assertIn('No restored candidates', reason)
        self.assertIn('assess_retirement', reason)
        self.assertNotIn('Explicit human designation required', reason)
        await bridge.handle(self.cleanup_request('check'))
        check_reason = output[-1]['result']['structuredContent']['reason']
        self.assertIn('No restored candidates', check_reason)
        self.assertIn('assess_retirement', check_reason)
        await bridge.handle(self.tabs_request('list'))
        receipt = output[-1]['result'].get('_meta', {}).get('com.workspace.neo/inventory_receipt')
        baseline = self.path.read_bytes()
        await bridge.handle(self.cleanup_request('assess_retirement', group_id='old-group',
                                                 inventory_receipt=receipt))
        assessment = output[-1]['result']['structuredContent']
        self.assertEqual(assessment['retirementStatus'], 'absent_at_assessment')
        self.assertEqual(assessment['retirementInput'],
                         dict(group_id='old-group', revision=73, target_ids=['old-target']))
        self.assertTrue(assessment['userAuthorizationRequired'])
        self.assertEqual(self.path.read_bytes(), baseline)
        await bridge.handle(self.cleanup_request(retirement=assessment['retirementInput']))
        self.assertEqual(output[-1]['result']['structuredContent']['decision'], 'block')
        self.assertEqual(self.path.read_bytes(), baseline)
        queued = stale.handle(self.tabs_request('new', url='about:blank'))
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        value = output[-1]['result']['structuredContent']
        self.assertEqual(value, dict(retiredGroupId='old-group', previousRevision=73, stateRevision=74,
                         absenceVerified=dict(nativeTabs=2,nativeTargets=2,nativeGroups=1),
                         freshInventoryRequired=True))
        self.assertEqual(bridge.task_state.record['pages'], [self.saved['pages'][1]])
        self.assertEqual(bridge.task_state.record['used_tabs'], [self.saved['used_tabs'][1]])
        self.assertEqual(bridge.task_state.record['outputs'], self.saved['outputs'])
        self.assertEqual(bridge.task_state.record['retired_bindings'][0]['removed']['retained'],
                         self.saved['retained'])
        self.assertEqual(bridge.task_state.record['retired_bindings'][0]['removed']['prior_bindings'],
                         self.saved['prior_bindings'])
        self.assertEqual(self.neighbor.read_text(encoding='utf-8'), 'unrelated untouched')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'retirement changed'):
            stale.task_state._write_record()
        self.assertEqual(self.path.read_bytes(), before)
        await queued
        self.assertIn('predates task retirement', json.dumps(stale_output[-1]))
        self.assertFalse(any(name == 'tabs' and args['action'] == 'new' for name,args in self.calls))
        await stale.handle(self.tabs_request('list'))
        receipt = stale_output[-1]['result']['_meta']['com.workspace.neo/inventory_receipt']
        await stale.handle(self.tabs_request('new', url='about:blank', inventory_receipt=receipt))
        self.assertFalse(stale_output[-1]['result'].get('isError'))
        self.assertEqual(sum(name == 'tabs' and args['action'] == 'new' for name,args in self.calls), 1)
        current = TaskState(self.root).bind(TASK)
        self.assertEqual({p['targetId'] for p in current['pages']}, {'sibling-target', 'fresh-target'})
        self.assertEqual(current['retired_bindings'][0]['removed']['pages'], [self.saved['pages'][0]])
        self.assertEqual(self.neighbor.read_text(encoding='utf-8'), 'unrelated untouched')
        bridge.starter.assert_not_awaited()
        stale.starter.assert_not_awaited()

    async def test_native_only_target_and_possible_restored_url_both_preserve_the_record(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        for target in (dict(targetId='old-target', url='https://example.com/moved', type='page'),
                       dict(targetId='new-physical', url=self.saved['pages'][0]['url'], type='page')):
            self.native_only = [target]
            await bridge.handle(self.cleanup_request(retirement=self.selection))
            self.assertIn('possible restored page still exists', json.dumps(output[-1]))
            self.assertEqual(self.path.read_bytes(), before)
        self.native_only = []
        self.groups.append(dict(id='old-group'))
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('still exists', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_revision_permission_and_inventory_checks_fail_closed(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        for changed in (dict(confirmed=False), dict(revision=72), dict(target_ids=['other-user'])):
            await bridge.handle(self.cleanup_request(retirement=dict(self.selection, **changed)))
            self.assertIn('"decision": "block"', json.dumps(output[-1]['result']['structuredContent']))
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.disagree = True
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('inventory sources disagree', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)
        self.disagree = False
        self.change_revision_during_proof = True
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('Task changed since verification', json.dumps(output[-1]))
        self.assertEqual(json.loads(self.path.read_bytes()), dict(self.saved, state_revision=74))

    async def test_same_tab_id_protects_live_tab_after_renderer_url_and_group_change(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        old = self.saved['pages'][0]
        self.pages.append(dict(targetId='replacement-renderer', tabId=old['tabId'], pageId=29,
                               groupId=None, windowId=30, url='https://example.com/changed-url', ownership='user'))
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('possible restored page still exists', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_mismatched_physical_alias_and_inventory_drift_preserve_record(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        def incorrect_alias(_, data):
            data['pages'] = [dict(data['pages'][0], tabId=35), *data['pages'][1:]]
        self.inventory_transform = incorrect_alias
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('physical tab identities disagree', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)
        def restored_between_reads(reads, data):
            if reads == 3:
                data['targets'].append(dict(targetId='restored-between-reads',
                    url=self.saved['pages'][0]['url'], type='page'))
        self.inventory_transform = restored_between_reads
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('possible restored page still exists', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)
        def other_change_between_reads(reads, data):
            if reads == 5:
                data['targets'].append(dict(targetId='new-native-background',
                    url='https://example.com/unrelated', type='service_worker'))
        self.inventory_transform = other_change_between_reads
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('Browser inventory changed during retirement', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_restore_during_final_endpoint_check_preserves_binding(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        guard = bridge.guard_endpoint

        async def guard_with_restore(*args, **kwargs):
            result = await guard(*args, **kwargs)
            if self.inventory_reads == 1:
                self.native_only = [dict(targetId='restoring-new-renderer', type='page', url='about:blank')]
            return result

        bridge.guard_endpoint = guard_with_restore
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('Unmapped browser target may be a restoring tab', json.dumps(output[-1]))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_incomplete_window_group_or_page_inventory_cannot_retire(self):
        self.setup_task()
        bridge, output = self.connect()
        before = self.path.read_bytes()
        cases = (
            lambda _, data: data['windows'][0].update(tabCount=3),
            lambda _, data: data['groups'][0].update(tabIds=[123]),
            lambda _, data: data['groups'][0].update(windowId=123),
            lambda _, data: data['targets'].append(dict(targetId='unmapped', type='page', url='about:blank')),
        )
        for case in cases:
            self.inventory_transform = case
            await bridge.handle(self.cleanup_request(retirement=self.selection))
            self.assertIn('"decision": "block"', json.dumps(output[-1]))
            self.assertEqual(self.path.read_bytes(), before)

    async def test_begin_reads_fresh_revision_from_a_second_connections_ordinary_write(self):
        self.setup_task()
        first, output = self.connect()
        writer, _ = self.connect()
        await first.handle(self.cleanup_request('begin'))
        self.assertEqual(output[-1]['result']['structuredContent']['stateRevision'], 73)
        writer.task_state.record['prior_bindings'].append(dict(self.saved['pages'][0],
            targetId='also-recorded', tabId=25, session='older-session'))
        writer.task_state._write_record()
        await first.handle(self.cleanup_request('begin'))
        observed = output[-1]['result']['structuredContent']
        self.assertEqual(observed['stateRevision'], 74)
        self.assertEqual(observed['recordedGroupTargets']['old-group'], ['also-recorded', 'old-target'])
        self.assertEqual(first.task_state.record['state_revision'], 73)
        self.assertEqual(self.calls, [])

    async def test_retirement_assessment_preserves_possible_live_tab_and_rejects_drift(self):
        for case in ('same-address', 'different-revision', 'inventory-drift', 'unknown-group'):
            with self.subTest(case=case):
                self.setup_task()
                if case == 'same-address':
                    # An ungrouped same-address tab is not a restored group candidate.
                    self.pages.append(dict(targetId='separate', tabId=41, pageId=41,
                                           groupId=None, url=self.saved['pages'][0]['url'],
                                           windowId=30, ownership='user'))
                if case == 'different-revision':
                    self.change_revision_during_proof = True
                if case == 'inventory-drift':
                    def change_between_reads(count, data):
                        if count == 2:
                            data['targets'].append(dict(targetId='new-service-worker',
                                                        type='service_worker', url='https://example.com/sw'))
                    self.inventory_transform = change_between_reads
                bridge, output = self.connect()
                await bridge.handle(self.tabs_request('list'))
                receipt = output[-1]['result'].get('_meta', {}).get('com.workspace.neo/inventory_receipt')
                before = self.path.read_bytes()
                group = 'unknown-group' if case == 'unknown-group' else 'old-group'
                await bridge.handle(self.cleanup_request('assess_retirement', group_id=group,
                                                         inventory_receipt=receipt))
                value = output[-1]['result']['structuredContent']
                self.assertEqual(value['decision'], 'block')
                self.assertNotIn('retirementInput', value)
                if case == 'different-revision':
                    self.assertEqual(json.loads(self.path.read_bytes()), dict(self.saved, state_revision=74))
                else:
                    self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual([args['action'] for name, args in self.calls if name == 'tabs'], ['list'])
                self.assertEqual(self.neighbor.read_text(encoding='utf-8'), 'unrelated untouched')

    async def test_abandon_archives_only_old_binding_and_preserves_same_url_tabs(self):
        self.setup_task()
        old = self.saved['pages'][0]
        same_address = [
            dict(old, targetId='restored-or-reopened', groupId='different-group',
                 tabId=41, pageId=41, windowId=30, ownership='user', ownerAgentId=None),
            dict(old, targetId='peer-target', groupId='peer-group',
                 tabId=42, pageId=42, windowId=30, ownership='other-agent', ownerAgentId='peer'),
        ]
        self.pages.extend(same_address)
        self.groups.extend(dict(id=p['groupId']) for p in same_address)
        visible_pages = copy.deepcopy(self.pages)
        bridge, output = self.connect()
        stale, stale_output = self.connect()
        await bridge.handle(self.cleanup_request(retirement=self.selection))
        self.assertIn('still exists', output[-1]['result']['structuredContent']['reason'])
        self.assertEqual(json.loads(self.path.read_bytes()), self.saved)
        queued = stale.handle(self.tabs_request('new', url='https://example.com/new'))
        await bridge.handle(self.cleanup_request('abandon', abandonment=self.selection))
        value = output[-1]['result']['structuredContent']
        self.assertEqual(value['abandonedGroupId'], 'old-group')
        self.assertEqual(value['stateRevision'], 74)
        self.assertFalse(value['browserTabsChanged'])
        self.assertTrue(value['freshInventoryRequired'])
        self.assertEqual(self.pages, visible_pages)
        record = json.loads(self.path.read_bytes())
        self.assertEqual(record['pages'], [self.saved['pages'][1]])
        self.assertEqual(record['outputs'], self.saved['outputs'])
        receipt = record['retired_bindings'][-1]
        self.assertEqual(receipt['disposition'], 'abandoned')
        self.assertEqual(receipt['removed']['pages'], [old])
        self.assertEqual(self.neighbor.read_text(encoding='utf-8'), 'unrelated untouched')
        # The two evidence snapshots are the only native calls during abandonment.
        self.assertEqual(self.inventory_reads, 3)  # rejected retire + two abandonment reads
        self.assertTrue(all('Browser.getTabs' in args['code'] for name, args in self.calls if name == 'run'))
        self.assertFalse(any(name == 'tabs' for name, _ in self.calls))
        await queued
        self.assertIn('predates task retirement', json.dumps(stale_output[-1]))
        await stale.handle(self.tabs_request('list'))
        inventory = stale_output[-1]['result']['_meta']['com.workspace.neo/inventory_receipt']
        await stale.handle(self.tabs_request('new', url='https://example.com/new', inventory_receipt=inventory))
        self.assertFalse(stale_output[-1]['result'].get('isError'))
        self.assertEqual(self.pages[:-1], visible_pages)

    async def test_abandon_rejects_original_ids_unverified_inventory_or_stale_record(self):
        for case in ('group', 'target', 'tab', 'stale', 'incomplete', 'drift', 'unconfirmed'):
            with self.subTest(case=case):
                self.setup_task()
                old = self.saved['pages'][0]
                if case in ('group', 'target', 'tab'):
                    current = dict(old, targetId='different-target', groupId='different-group',
                                   tabId=41, pageId=41, windowId=30, ownership='user')
                    key = dict(group='groupId', target='targetId', tab='tabId')[case]
                    current[key] = old[key]
                    self.pages.append(current)
                    self.groups.append(dict(id=current['groupId']))
                if case == 'incomplete':
                    self.disagree = True
                if case == 'drift':
                    def change_between_reads(count, data):
                        if count == 2:
                            data['targets'].append(dict(targetId='new-service-worker',
                                                        type='service_worker', url='https://example.com/sw'))
                    self.inventory_transform = change_between_reads
                bridge, output = self.connect()
                selection = dict(self.selection, revision=72 if case == 'stale' else 73,
                                 confirmed=case != 'unconfirmed')
                before = self.path.read_bytes()
                await bridge.handle(self.cleanup_request('abandon', abandonment=selection))
                self.assertIn('block', json.dumps(output[-1]))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(any(name == 'tabs' for name, _ in self.calls))


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
                      'structuredContent': {'ok': True, 'value': {'pages': self.pages, 'groups': self.groups, 'targets': [p['targetId'] for p in self.pages]}}}
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
                         [('run', 'const pages=await browser.pages.list(); const targets=(await browser.cdp("Target.getTargets",{})).targetInfos.map(t=>t.targetId); const groups=(await browser.cdp("Browser.getTabGroups",{})).groups; return {pages,groups,targets};')])
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
