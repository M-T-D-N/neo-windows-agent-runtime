"""Readonly cleanup must preserve original recovery evidence on a new browser session."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from neo_mcp_bridge import Bridge,SESSION_META_KEY,UpstreamError
TASK='00000000-0000-4000-8000-000000000001'
OLD=dict(pageId=1,targetId='original-target',tabId=101,groupId='original-group',windowId=10,url='https://example.com/draft',ownership='mine')
NEW=dict(pageId=2,targetId='restored-target',tabId=202,groupId='renamed-group',windowId=20,url=OLD['url'],ownership='user',ownerAgentId=None)
OLD_GROUP=dict(groupId='original-group',title='original title',tabIds=[101])
NEW_GROUP=dict(groupId='renamed-group',title='human renamed',tabIds=[202])

def request(action,**args):return dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='workspace_cleanup',arguments=dict(action=action,task_id=TASK,**args)))

class RestorationEvidenceTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=os.environ['WEB_AUTOMATION_TEST_ROOT']);self.addCleanup(self.temp.cleanup)
 def make(self,name,mode='renamed',*,checkpoint=False,late=False,same_session=False):
  root=Path(self.temp.name)/name;output=[];sent=[];observed='old-session' if same_session else 'new-session';count=[0]
  def inventory():
   count[0]+=1
   if late and count[0]==1:return dict(pages=[OLD],groups=[OLD_GROUP],targets=[OLD['targetId']])
   if mode=='empty':return dict(pages=[],groups=[],targets=[])
   if mode=='partial':return dict(pages=[],groups=[NEW_GROUP],targets=[NEW['targetId']])
   if mode=='incomplete':return dict(pages=[NEW],groups=[NEW_GROUP])
   return dict(pages=[NEW],groups=[NEW_GROUP],targets=[NEW['targetId']])
  def handler(http):
   rpc=json.loads(http.content);sent.append(rpc);self.assertEqual(rpc['params']['name'],'run');code=rpc['params']['arguments']['code']
   if 'Browser.getTabGroups' in code:value=inventory()
   elif 'Browser.getTabs' in code:value=dict(pages=[NEW],tabs=[dict(targetId=NEW['targetId'],tabId=202,url=NEW['url'])],targets=[NEW['targetId']])
   else:value=[] if mode=='empty' else [NEW]
   return httpx.Response(200,json=dict(jsonrpc='2.0',id=rpc['id'],result=dict(structuredContent=dict(ok=True,value=value),_meta={SESSION_META_KEY:observed})))
  b=Bridge('http://127.0.0.1:9010/mcp',output.append,state_root=root,task_id=TASK,client=httpx.AsyncClient(transport=httpx.MockTransport(handler)));self.addAsyncCleanup(b.close)
  retained=[(('old-session',OLD['targetId'],OLD['url']),'Preserve original recovery evidence')]
  b.task_state.save('old-session',[OLD],[OLD_GROUP],retained);b.cleanup.retained=dict(retained);b.tool_session=observed;b.execution_id='current-execution'
  if checkpoint:b.task_state.checkpoint_session(observed)
  path=root/(TASK+'.json');return b,output,path,sent
 def recoverable(self,b):
  b.task_state.checkpoint_session('new-session')
  recovery=dict(prior_group_id='original-group',group_id='renamed-group',pages=[dict(targetId=NEW['targetId'],url=NEW['url'])],reason='Human explicitly selected this restored synthetic group')
  proof=b.task_state.restored_recovery(recovery,[NEW],[NEW_GROUP]);self.assertEqual(proof['priorSession'],'old-session');self.assertEqual(proof['prior'][0]['targetId'],OLD['targetId'])
 async def test_cleanup_check_use_defer_preserve_bytes_before_and_after_checkpoint(self):
  for checkpoint in [False,True]:
   for mode in ['renamed','empty','partial','incomplete']:
    for action in ['check','use','defer']:
     with self.subTest(checkpoint=checkpoint,mode=mode,action=action):
      b,out,path,sent=self.make(f'{checkpoint}-{mode}-{action}',mode,checkpoint=checkpoint);before=path.read_bytes();cache=copy.deepcopy(b.task_state.record);retained=copy.deepcopy(b.cleanup.retained)
      args={} if action=='check' else dict(pageIds=[2],reason='Observe restored scope without losing original evidence')
      if action=='defer':args['condition']='other_owner'
      await b.handle(request(action,**args));value=out[-1]['result']['structuredContent'];self.assertEqual(value.get('decision'),'block');self.assertEqual(path.read_bytes(),before);self.assertEqual(b.task_state.record,cache);self.assertEqual(b.cleanup.retained,retained);self.assertTrue(all(x['params']['name']=='run' for x in sent));self.recoverable(b)
 async def test_late_restored_snapshot_cannot_retire_retention_or_mark_completion(self):
  b,out,path,sent=self.make('late',late=True);before=path.read_bytes();retained=copy.deepcopy(b.cleanup.retained)
  await b.handle(request('check'));self.assertEqual(out[-1]['result']['structuredContent'].get('decision'),'block');self.assertEqual(path.read_bytes(),before);self.assertEqual(b.cleanup.retained,retained);self.recoverable(b)
 async def test_remember_task_rejects_restoration_before_modifying_or_saving_record(self):
  b,out,path,sent=self.make('remember');before=path.read_bytes();cache=copy.deepcopy(b.task_state.record)
  with self.assertRaises(UpstreamError):await b.remember_task()
  self.assertEqual(path.read_bytes(),before);self.assertEqual(b.task_state.record,cache);self.recoverable(b)
 async def test_direct_save_cannot_discard_unresolved_original_binding(self):
  b,out,path,sent=self.make('save');before=path.read_bytes();cache=copy.deepcopy(b.task_state.record)
  with self.assertRaises(ValueError):b.task_state.save('new-session',[NEW],[NEW_GROUP])
  self.assertEqual(path.read_bytes(),before);self.assertEqual(b.task_state.record,cache);self.recoverable(b)
 async def test_save_recovery_requires_completed_native_ownership(self):
  b,out,path,sent=self.make('selected-save');before=path.read_bytes()
  recovery=dict(prior_group_id='original-group',group_id='renamed-group',pages=[dict(targetId=NEW['targetId'],url=NEW['url'])],reason='Human explicitly selected the restored synthetic group')
  with self.assertRaises(ValueError):b.task_state.save('new-session',[NEW],[NEW_GROUP],recovery=recovery)
  self.assertEqual(path.read_bytes(),before)
  owned=dict(NEW,ownership='mine')
  with self.assertRaises(ValueError):b.task_state.save('new-session',[owned,dict(NEW,ownership='other-agent',ownerAgentId='active')],[NEW_GROUP],recovery=recovery)
  self.assertEqual(path.read_bytes(),before)
  b.task_state.save('new-session',[owned],[NEW_GROUP],recovery=recovery)
  self.assertEqual(b.task_state.record['pages'][0]['targetId'],NEW['targetId'])
 async def test_complete_absence_in_original_session_still_retires_known_closed_scope(self):
  b,out,path,sent=self.make('closed','empty',same_session=True);await b.handle(request('check'));self.assertNotEqual(out[-1]['result']['structuredContent'].get('decision'),'block');self.assertEqual(json.loads(path.read_text())['pages'],[])

 async def test_multi_group_alias_and_partial_survival_preserve_exact_binding(self):
  from unittest.mock import AsyncMock
  second=dict(OLD,pageId=3,targetId='second-original',tabId=103,groupId='second-group')
  second_group=dict(groupId='second-group',title='second original',tabIds=[103])
  owned=dict(NEW,ownership='mine')
  foreign=dict(owned,pageId=4,groupId='foreign-group',ownership='other-agent',ownerAgentId='another-active-client')
  cases={
   'same-url-survivor':([second,NEW],[second_group,NEW_GROUP]),
   'unrelated-owned-same-url':([owned],[NEW_GROUP]),
   'foreign-alias':([owned,foreign],[NEW_GROUP,dict(groupId='foreign-group',tabIds=[202])]),
   'partial-original':([OLD],[OLD_GROUP]),
  }
  for name,(pages,groups) in cases.items():
   for action in ['check','use','defer']:
    with self.subTest(case=name,action=action):
     b,out,path,sent=self.make(name+'-'+action)
     old_pages=[OLD,second] if name in ('same-url-survivor','partial-original') else [OLD]
     old_groups=[OLD_GROUP,second_group] if len(old_pages)==2 else [OLD_GROUP]
     b.task_state.save('old-session',old_pages,old_groups,b.cleanup.retained.items())
     b.inventory=AsyncMock(return_value=dict(pages=pages,groups=groups,targets=list({p['targetId'] for p in pages})))
     before=path.read_bytes();cache=copy.deepcopy(b.task_state.record);retained=copy.deepcopy(b.cleanup.retained)
     args={} if action=='check' else dict(pageIds=[pages[0]['pageId']],reason='Inspect without acknowledging unrelated restoration')
     if action=='defer':args['condition']='other_owner'
     await b.handle(request(action,**args))
     self.assertEqual(out[-1]['result']['structuredContent'].get('decision'),'block')
     self.assertEqual(path.read_bytes(),before);self.assertEqual(b.task_state.record,cache);self.assertEqual(b.cleanup.retained,retained)
     with self.assertRaises(ValueError):b.task_state.save('new-session',pages,groups,native_targets=[p['targetId'] for p in pages])
     self.assertEqual(path.read_bytes(),before)

 async def test_selected_recovery_discharges_only_its_prior_group(self):
  second=dict(OLD,pageId=3,targetId='second-original',tabId=103,groupId='second-group')
  second_group=dict(groupId='second-group',title='second original',tabIds=[103])
  owned=dict(NEW,ownership='mine')
  recovery=dict(prior_group_id='original-group',group_id='renamed-group',pages=[dict(targetId=NEW['targetId'],url=NEW['url'])],reason='Human selected only original-group')
  for survivor in [False,True]:
   with self.subTest(survivor=survivor):
    b,out,path,sent=self.make('selected-multi-'+str(survivor));b.task_state.save('old-session',[OLD,second],[OLD_GROUP,second_group]);before=path.read_bytes()
    pages=[owned,second] if survivor else [owned];groups=[NEW_GROUP,second_group] if survivor else [NEW_GROUP]
    if not survivor:
     with self.assertRaises(ValueError):b.task_state.save('new-session',pages,groups,recovery=recovery)
     self.assertEqual(path.read_bytes(),before)
    else:
     b.task_state.save('new-session',pages,groups,recovery=recovery)
     self.assertEqual({p['targetId'] for p in b.task_state.record['pages']},{NEW['targetId'],second['targetId']})
  b,out,path,sent=self.make('selected-foreign');b.task_state.save('old-session',[OLD,second],[OLD_GROUP,second_group]);before=path.read_bytes()
  foreign=dict(owned,pageId=4,groupId='outside',ownership='other-agent',ownerAgentId='active')
  with self.assertRaises(ValueError):b.task_state.save('new-session',[owned,second,foreign],[NEW_GROUP,second_group,dict(groupId='outside')],recovery=recovery)
  self.assertEqual(path.read_bytes(),before)
