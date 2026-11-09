import json
import os
from pathlib import Path
import tempfile
import unittest
from neo_task_state import TaskState

TASK='00000000-0000-4000-8000-000000000001'
def page(target='one',group='original',ownership='mine',url='https://example.com/a'):
    return dict(pageId=1,targetId=target,groupId=group,url=url,ownership=ownership,ownerAgentId=None)

class TaskStateTests(unittest.TestCase):
    def setUp(self):
        root=os.environ['WEB_AUTOMATION_TEST_ROOT']
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.state=TaskState(self.root)
        self.state.bind(TASK)
        self.state.save('old',[page()],[dict(id='original',title='verified task label')])

    def test_reconnect_loads_same_handle_and_can_reuse_without_new_tab(self):
        fresh=TaskState(self.root);record=fresh.bind(TASK)
        self.assertEqual(record['session'],'old')
        self.assertEqual(fresh.reconciliation([page()],[dict(id='original',title='verified task label')]),([],[]))

    def test_restart_requires_unique_saved_group_and_complete_membership(self):
        fresh=TaskState(self.root);fresh.bind(TASK)
        restored=page('new','restored','user')
        groups=[dict(id='restored',title='verified task label')]
        self.assertEqual(fresh.reconciliation([restored],groups),(['restored'],[]))
        duplicate=page('different','other','user')
        self.assertEqual(fresh.reconciliation([restored,duplicate],groups)[0],['restored'])
        groups.append(dict(id='other',title='verified task label'))
        self.assertTrue(fresh.reconciliation([restored,duplicate],groups)[1])

    def test_already_adopted_restored_group_reuses_new_targets_without_reclaim(self):
        restored=page('new','restored','mine')
        alias=dict(restored,ownership='user',pageId=7)
        groups=[dict(id='restored',title='verified task label')]
        self.assertEqual(self.state.reconciliation([restored,alias],groups),([],[]))
        other=dict(alias,ownership='other-agent',ownerAgentId='different-owner')
        self.assertTrue(self.state.reconciliation([restored,other],groups)[1])

    def test_active_other_owner_and_mixed_members_never_claimed(self):
        groups=[dict(id='original',title='verified task label')]
        other=page(ownership='other-agent');other['ownerAgentId']='active-other'
        self.assertTrue(self.state.reconciliation([other],groups)[1])
        extra=page('two',url='https://example.com/b',ownership='user')
        self.assertTrue(self.state.reconciliation([page(ownership='user'),extra],groups)[1])

    def test_verified_partial_close_keeps_surviving_owned_target(self):
        groups=[dict(id='original',title='verified task label')]
        self.state.save('old',[page(),page('temporary',url='about:blank')],groups)
        self.assertEqual(self.state.reconciliation([page()],groups),([],[]))
        self.assertEqual(len(self.state.record['pages']),2)
        self.assertTrue(self.state.reconciliation([page(ownership='other-agent')],groups)[1])
        self.assertTrue(self.state.reconciliation([page(ownership='user')],groups)[1])

    def test_owned_replacement_in_same_exact_group_is_not_title_based_recovery(self):
        groups=[dict(id='original',title='verified task label')]
        replacement=page('replacement',url='https://example.com/replacement')
        self.assertEqual(self.state.reconciliation([replacement],groups),([],[]))
        self.assertTrue(self.state.reconciliation([dict(replacement,ownership='user')],groups)[1])
        self.assertTrue(self.state.reconciliation([replacement,dict(replacement,ownership='other-agent')],groups)[1])
        unrelated=dict(replacement,groupId='other')
        self.assertTrue(self.state.reconciliation([unrelated],[dict(id='other',title='verified task label')])[1])

    def test_same_physical_mine_alias_does_not_override_another_active_owner(self):
        mine=page()
        other=dict(mine,pageId=8,ownership='other-agent',ownerAgentId='active-other')
        self.assertTrue(self.state.reconciliation([mine,other],[dict(id='original',title='verified task label')])[1])

    def test_same_url_alone_never_claims_and_closed_tab_is_not_resurrected(self):
        self.assertEqual(self.state.reconciliation([page('unrelated','other','user')],[dict(id='other',title='unrelated')]),([],[]))
        self.assertEqual(self.state.reconciliation([],[]),([],[]))

    def test_explicit_recovery_requires_exact_members_and_no_other_owner(self):
        restored=page('new','restored','user','https://example.com/login')
        group=dict(id='restored',title='verified task label')
        recovery=dict(prior_group_id='original',group_id='restored',
                      pages=[dict(targetId='new',url=restored['url'])],
                      reason='User explicitly selected this restored task group')
        self.assertTrue(self.state.reconciliation([restored],[group])[1])
        self.assertEqual(self.state.reconciliation([restored],[group],recovery=recovery),(['restored'],[]))
        self.assertEqual(self.state.record['pages'][0]['targetId'],'one')
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored,page('extra','restored','user')],[group],recovery=recovery)
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored,dict(restored,ownership='other-agent')],[group],recovery=recovery)
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored,dict(restored,ownership='other-agent',groupId='elsewhere')],[group],recovery=recovery)
        with self.assertRaises(ValueError):
            self.state.reconciliation([dict(restored,url='https://example.com/changed')],[group],recovery=recovery)
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored,page('one','elsewhere','user')],[group],recovery=recovery)
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored],[group],recovery=dict(recovery,prior_group_id='unrecorded'))
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored],[group],recovery=dict(recovery,reason=''))
        with self.assertRaises(ValueError):
            self.state.reconciliation([restored],[group],recovery=dict(recovery,pages=[dict(targetId='new',url='https://example.com/?token=secret')]))
        alias=dict(restored,ownership='mine',pageId=7)
        self.assertEqual(self.state.reconciliation([restored,alias],[group],recovery=recovery),([],[]))

    def test_corrupt_future_and_other_task_records_fail_closed(self):
        path=self.root/(TASK+'.json')
        path.write_text('{',encoding='utf-8')
        with self.assertRaises(ValueError):TaskState(self.root).bind(TASK)
        path.write_text(json.dumps(dict(schema_version=2,task_id=TASK)),encoding='utf-8')
        with self.assertRaises(ValueError):TaskState(self.root).bind(TASK)
        with self.assertRaises(ValueError):self.state.bind('00000000-0000-4000-8000-000000000002')


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_reclaim_verifies_ownership_without_creating_a_tab(self):
        from unittest.mock import AsyncMock
        from neo_mcp_bridge import Bridge, UpstreamError
        for adopted in (True, False):
            with self.subTest(adopted=adopted), tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
                state = TaskState(root); state.bind(TASK)
                state.save('old', [page()], [dict(id='original', title='verified task label')])
                bridge = Bridge('http://127.0.0.1:9010/mcp', lambda _: None, state_root=root, task_id=TASK,
                    catalog={'catalog': {'tools': [{'name': 'tabs', 'inputSchema': {
                        'properties': {'action': {'enum': ['list', 'new', 'close', 'reclaim']}}}}]}})
                self.addAsyncCleanup(bridge.close)
                restored = page('new', 'restored', 'user')
                group = dict(id='restored', title='verified task label')
                after = dict(restored, ownership='mine') if adopted else restored
                bridge.inventory = AsyncMock(side_effect=[dict(pages=[restored, dict(restored)], groups=[group]),
                                                          dict(pages=[after], groups=[group])])
                bridge.internal_tool = AsyncMock(return_value={})
                if adopted:
                    await bridge.prepare_task()
                    self.assertEqual(bridge.task_state.record['pages'][0]['targetId'], 'new')
                else:
                    with self.assertRaisesRegex(UpstreamError, 'remains unverified'):
                        await bridge.prepare_task()
                    self.assertEqual(bridge.task_state.record['pages'][0]['targetId'], 'one')
                    self.assertFalse(bridge.task_prepared)
                bridge.internal_tool.assert_awaited_once_with('tabs', {'action': 'reclaim', 'groupId': 'restored',
                    'expected': [{'targetId': 'new', 'url': restored['url']}]})

    async def test_detached_adoption_waits_and_removes_only_one_temporary_tab(self):
        from unittest.mock import AsyncMock
        from neo_mcp_bridge import Bridge
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            state=TaskState(root); state.bind(TASK)
            state.save('old',[page()],[dict(id='original',title='verified task label')])
            bridge=Bridge('http://127.0.0.1:9010/mcp',lambda _:None,state_root=root,task_id=TASK)
            self.addAsyncCleanup(bridge.close)
            restored=page('new','restored','user')
            owned=dict(restored,ownership='mine')
            temporary=dict(page('temporary','restored','mine','about:blank'),pageId=9)
            group=dict(id='restored',title='verified task label')
            bridge.inventory=AsyncMock(side_effect=[
                dict(pages=[restored],groups=[group]),
                dict(pages=[restored,temporary,dict(temporary)],groups=[group]),
                dict(pages=[owned,temporary],groups=[group]),
                dict(pages=[owned],groups=[group])])
            bridge.internal_tool=AsyncMock(return_value={})
            await bridge.prepare_task()
            self.assertEqual(bridge.internal_tool.await_count,2)
            self.assertEqual(bridge.internal_tool.await_args_list[0].args,('tabs',{'action':'new','groupId':'restored','url':'about:blank'}))
            self.assertEqual(bridge.internal_tool.await_args_list[1].args,('tabs',{'action':'close','page':9}))
            self.assertEqual(bridge.task_state.record['pages'][0]['targetId'],'new')

    async def test_explicit_resume_uses_native_adoption_and_cleans_temporary(self):
        from unittest.mock import AsyncMock
        from neo_mcp_bridge import Bridge
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            state=TaskState(root);state.bind(TASK)
            state.save('old',[page()],[dict(id='original',title='verified task label')])
            out=[];bridge=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=TASK)
            self.addAsyncCleanup(bridge.close)
            restored=page('new','restored','user','https://example.com/login')
            owned=dict(restored,ownership='mine')
            temporary=dict(page('temporary','restored','mine','about:blank'),pageId=9)
            group=dict(id='restored',title='verified task label')
            bridge.inventory=AsyncMock(return_value=dict(pages=[restored],groups=[group]))
            await bridge.handle({'jsonrpc':'2.0','id':0,'method':'tools/call',
                'params':{'name':'workspace_cleanup','arguments':dict(action='inventory',task_id=TASK,group_id='restored')}})
            metadata=out[-1]['result']['structuredContent']
            self.assertEqual(metadata['recoveryPages'],[dict(targetId='new',url=restored['url'])])
            self.assertEqual(bridge.task_state.record['pages'][0]['targetId'],'one')
            recovery=dict(prior_group_id='original',group_id='restored',
                          pages=metadata['recoveryPages'],
                          reason='User selected the exact restored synthetic group')
            bridge.ensure_ready=AsyncMock()
            bridge.inventory=AsyncMock(side_effect=[dict(pages=[restored],groups=[group]),
                dict(pages=[owned,temporary],groups=[group]),dict(pages=[owned],groups=[group]),
                dict(pages=[owned],groups=[group])])
            bridge.internal_tool=AsyncMock(return_value={})
            # Exercise the public dispatch path, with only the trailing check
            # response stubbed so it cannot hide adoption/cleanup behavior.
            async def check_reply(request, **kwargs):
                return {'result':{'structuredContent':{'ok':True,'value':[owned]}}}
            bridge.post=check_reply
            bridge.remember_task=AsyncMock()
            bridge.tool_session='new-session'
            await bridge.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'workspace_cleanup','arguments':dict(action='resume',task_id=TASK,recovery=recovery)}})
            self.assertNotIn('ownership reconciliation',out[-1]['result']['structuredContent'].get('reason',''))
            self.assertEqual(out[-1]['result']['structuredContent']['pendingPageIds'],[1])
            self.assertEqual(bridge.internal_tool.await_args_list[0].args,
                             ('tabs',{'action':'new','groupId':'restored','url':'about:blank'}))
            self.assertEqual(bridge.internal_tool.await_args_list[1].args,('tabs',{'action':'close','page':9}))
            self.assertEqual(bridge.task_state.record['pages'][0]['targetId'],'new')

    async def test_failed_adoption_closes_temporary_and_preserves_prior_record(self):
        from unittest.mock import AsyncMock, patch
        from neo_mcp_bridge import Bridge, UpstreamError
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            state=TaskState(root);state.bind(TASK)
            state.save('old',[page()],[dict(id='original',title='verified task label')])
            bridge=Bridge('http://127.0.0.1:9010/mcp',lambda _:None,state_root=root,task_id=TASK)
            self.addAsyncCleanup(bridge.close)
            restored=page('new','restored','user'); group=dict(id='restored',title='verified task label')
            temporary=dict(page('temporary','restored','mine','about:blank'),pageId=9)
            bridge.inventory=AsyncMock(side_effect=[dict(pages=[restored],groups=[group]),dict(pages=[restored,temporary],groups=[group]),dict(pages=[restored],groups=[group])])
            bridge.internal_tool=AsyncMock(return_value={})
            with patch('neo_mcp_bridge.time.monotonic',side_effect=[0,6]):
                with self.assertRaisesRegex(UpstreamError,'not adopted'):
                    await bridge.prepare_task(recovery=dict(prior_group_id='original',group_id='restored',
                        pages=[dict(targetId='new',url=restored['url'])],
                        reason='User selected the restored group for failure verification'))
            self.assertEqual(bridge.internal_tool.await_args_list[-1].args,('tabs',{'action':'close','page':9}))
            self.assertEqual(bridge.task_state.record['pages'][0]['targetId'],'one')

    async def test_failed_resume_is_recorded_for_this_agy_execution(self):
        from unittest.mock import AsyncMock
        from neo_mcp_bridge import Bridge, UpstreamError
        with tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']) as root:
            out=[]
            bridge=Bridge('http://127.0.0.1:9010/mcp',out.append,state_root=root,task_id=TASK)
            self.addAsyncCleanup(bridge.close)
            bridge.execution_id='current'
            bridge.ensure_ready=AsyncMock(side_effect=UpstreamError('runtime unavailable'))
            await bridge.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'workspace_cleanup','arguments':{'action':'resume','task_id':TASK}}})
            record=json.loads((Path(root)/(TASK+'.json')).read_text(encoding='utf-8'))
            self.assertEqual(record['execution_id'],'current')
            self.assertTrue(record['execution_incomplete'])
            self.assertEqual(out[-1]['result']['structuredContent']['decision'],'block')


class OutputOwnershipTests(unittest.TestCase):
    def setUp(self):
        import time
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output_root = self.root / 'tool-output'
        self.output_root.mkdir()
        self.state = TaskState(self.root / 'tasks', output_root=self.output_root)
        self.state.bind(TASK)
        self.baseline = self.state.output_baseline('evaluate')
        self.start = time.time_ns()

    def output(self, producer='evaluate', extension='txt'):
        import time, uuid
        path = self.output_root / f'{producer}-{time.time_ns() // 1000000}-{uuid.uuid4()}.{extension}'
        path.write_text('synthetic output', encoding='utf-8')
        return path

    def reply(self, path):
        return {'structuredContent':dict(path=str(path), writtenToFile=True, contentLength=16, page=1)}

    def test_real_file_identity_survives_reconnect_and_absence_is_explicit(self):
        path = self.output()
        self.assertTrue(self.state.record_output('evaluate', self.reply(path), self.start, 'rpc-1', 'session-a', self.baseline))
        fresh = TaskState(self.root / 'tasks', output_root=self.output_root)
        fresh.bind(TASK)
        before = (self.root / 'tasks' / (TASK+'.json')).read_bytes()
        info = fresh.outputs()
        self.assertEqual(info['outputs'][0]['status'], 'present')
        self.assertEqual(info['present_bytes'], path.stat().st_size)
        self.assertEqual(info['outputs'][0]['request_id'], 'rpc-1')
        path.unlink()  # this test owns the synthetic fixture
        self.assertEqual(fresh.outputs()['outputs'][0]['status'], 'absent')
        self.assertEqual((self.root / 'tasks' / (TASK+'.json')).read_bytes(), before)

    def test_existing_forged_path_is_not_claimed(self):
        import time
        path = self.output()
        later = max(time.time_ns(), path.stat().st_birthtime_ns + 1000)
        with self.assertRaisesRegex(ValueError, 'existed before dispatch'):
            self.state.record_output('evaluate', self.reply(path), later, 2, 's', self.state.output_baseline('evaluate'))
        self.assertNotIn('outputs', self.state.record)

    @unittest.skipUnless(os.name == 'nt', 'Windows canonical path spelling')
    def test_extended_length_path_keeps_exact_directory_identity(self):
        path = self.output('evaluate_in_page')
        extended = Path('\\\\?\\' + str(path.resolve()))
        self.assertTrue(self.state.record_output('evaluate_in_page', self.reply(extended),
                                                self.start, 2, 's', frozenset()))
        self.assertEqual(self.state.outputs()['outputs'][0]['status'], 'present')
        outside = self.root / path.name
        outside.write_text('foreign output', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.state.record_output('evaluate_in_page',
                self.reply(Path('\\\\?\\' + str(outside.resolve()))), self.start, 3, 's', frozenset())

    def test_changed_file_identity_remains_visible_and_is_not_retired(self):
        path = self.output()
        self.state.record_output('evaluate', self.reply(path), self.start, 3, 's', self.baseline)
        path.write_text('changed synthetic payload with different size', encoding='utf-8')
        info = self.state.outputs()
        self.assertEqual(info['outputs'][0]['status'], 'identity_changed')
        self.assertTrue(path.exists())
        self.assertEqual(info['present_bytes'], 0)

    def test_untrusted_nested_value_download_pdf_and_wrong_root_are_excluded(self):
        path = self.output()
        nested = {'structuredContent':{'page':1,'value':self.reply(path)['structuredContent']}}
        self.assertFalse(self.state.record_output('evaluate', nested, self.start, 4, 's', self.baseline))
        self.assertFalse(self.state.record_output('download', self.reply(path), self.start, 4, 's', self.baseline))
        pdf = self.output('evaluate', 'pdf')
        with self.assertRaises(ValueError):
            self.state.record_output('evaluate', self.reply(pdf), self.start, 4, 's', self.baseline)
        outside = self.root / path.name
        outside.write_text('foreign file', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'outside'):
            self.state.record_output('evaluate', self.reply(outside), self.start, 4, 's', self.baseline)
        self.assertNotIn('outputs', self.state.record)

    def test_grep_requires_actual_direct_metadata_shape(self):
        path = self.output('grep')
        fields = dict(path=str(path), truncated=True, page=2, over='ax', count=1)
        self.assertTrue(self.state.record_output('grep', {'structuredContent':fields}, self.start, 5, 's', frozenset()))
        another = self.output('grep')
        for altered in ({'over':'invented'}, {'count':True}, {'truncated':False}, {'outputWriteFailed':True}):
            self.assertFalse(self.state.record_output('grep', {'structuredContent':dict(fields,path=str(another),**altered)}, self.start, 6, 's', frozenset()))

    def test_save_failure_does_not_claim_unpersisted_output(self):
        from unittest.mock import patch
        path = self.output()
        with patch('neo_task_state.atomic_write_json', side_effect=OSError('synthetic save failure')):
            with self.assertRaisesRegex(OSError, 'save failure'):
                self.state.record_output('evaluate', self.reply(path), self.start, 7, 's', self.baseline)
        self.assertNotIn('outputs', self.state.record)
        self.assertTrue(path.exists())


    def test_direct_read_diff_snapshot_shapes_use_proven_top_level_metadata(self):
        for producer, extension in [('read','md'), ('read','txt'), ('diff','md'), ('snapshot','md'), ('evaluate_in_page','txt')]:
            with self.subTest(producer=producer, extension=extension):
                baseline = self.state.output_baseline(producer)
                path = self.output(producer, extension)
                self.assertTrue(self.state.record_output(producer,self.reply(path),self.start,8,'s',baseline))
        self.assertEqual(len(self.state.record['outputs']),5)
