import unittest
from neo_cleanup import CleanupPolicy

TASK='00000000-0000-4000-8000-000000000001'
def args(action='check',**kw):return dict(action=action,task_id=TASK,**kw)
def alias(pid=1,owner='user',target='physical'):
    return dict(pageId=pid,targetId=target,tabId=7,url='https://stale.example/',ownership=owner,ownerAgentId=None)
def tab(target='physical',url='https://example.com/document'):
    return dict(targetId=target,tabId=7,windowId=3,url=url)

class UsedTabsTests(unittest.TestCase):
    def review(self,policy,pages,action='check',tabs=None,targets=None,**kw):
        return policy.review('s',pages,args(action,**kw),physical_tabs=[tab()] if tabs is None else tabs,
                             targets=['physical'] if targets is None else targets)

    def test_readonly_inventory_does_not_enroll_but_explicit_use_blocks_finish(self):
        p=CleanupPolicy(); pages=[alias()]
        self.assertNotIn('usedPages',self.review(p,pages))
        self.review(p,pages,'use',pageIds=[1],reason='Reused editor for approved document work')
        result=self.review(p,pages)
        self.assertEqual(result['decision'],'block')
        self.assertEqual(result['pendingPageIds'],[])
        self.assertEqual(result['pendingUsedTargetIds'],['physical'])
        self.assertEqual(result['usedPages'][0]['url'],tab()['url'])
        self.assertFalse(result['usedPages'][0]['closeAllowed'])
        self.assertEqual(result['usedPages'][0]['closeAuthority'],'permission_missing')

    def test_aliases_deduplicate_and_other_owner_overrides_mine(self):
        p=CleanupPolicy(); pages=[alias(),alias(2,'mine'),dict(alias(3,'other-agent'),ownerAgentId='active')]
        self.review(p,pages,'use',pageIds=[1,2],reason='Actual synthetic editor reuse')
        result=self.review(p,pages)
        self.assertEqual(len(result['usedPages']),1)
        self.assertEqual(result['pendingUsedTargetIds'],['physical'])
        self.assertEqual(result['usedPages'][0]['closeAuthority'],'other_owner')
        self.assertFalse(result['usedPages'][0]['closeAllowed'])
        self.assertEqual(result['pendingPageIds'],[])

    def test_concrete_deferral_preserves_unsaved_and_requires_fresh_boundary_review(self):
        p=CleanupPolicy(); pages=[alias(1,'other-agent')]
        self.review(p,pages,'use',pageIds=[1],reason='Actual synthetic document reuse')
        self.review(p,pages,'defer',pageIds=[1],condition='unsaved',reason='Document model still contains unsaved draft; leave open')
        result=self.review(p,pages,stop_hook_active=False)
        self.assertEqual(result['cleanupStatus'],'deferred')
        self.assertEqual(result['deferredUsedPages'][0]['condition'],'unsaved')
        self.assertNotIn('decision',result)
        self.assertEqual(self.review(p,pages)['decision'],'block')
        self.review(p,pages,'defer',pageIds=[1],condition='other_owner',reason='Other active owner is still writing this document')
        changed=[dict(pages[0],ownerAgentId='replacement-owner')]
        self.assertEqual(self.review(p,changed)['pendingUsedTargetIds'],['physical'])

    def test_only_complete_physical_absence_clears_scope_not_alias_loss(self):
        p=CleanupPolicy()
        self.review(p,[alias()],'use',pageIds=[1],reason='Reused actual physical document')
        self.assertEqual(self.review(p,[])['decision'],'block')
        with self.assertRaises(ValueError):
            p.review('s',[],args(),physical_tabs=None,targets=[])
        self.assertTrue(p.used)
        self.assertEqual(self.review(p,[],tabs=[],targets=[])['usedPages'],[])
        self.assertFalse(p.used)

    def test_new_target_in_same_tab_requires_explicit_reuse_and_bad_input_no_mutation(self):
        p=CleanupPolicy()
        self.review(p,[alias()],'use',pageIds=[1],reason='Reused actual physical document')
        result=self.review(p,[alias(target='replacement')],tabs=[tab('replacement')],targets=['replacement'])
        self.assertEqual(result['usedPages'][0]['closeAuthority'],'identity_changed')
        with self.assertRaises(ValueError):
            self.review(p,[alias()],'defer',pageIds=[1],condition='saved',reason='Blank fields are not proof of saved state')
        self.assertTrue(p.used)
        with self.assertRaises(ValueError):
            self.review(p,[alias()],'use',pageIds=[9],reason='Do not enroll unknown alias')
        with self.assertRaises(ValueError):
            self.review(p,[alias()],'use',pageIds=[1],reason='Do not save credential bearing URLs',tabs=[tab(url='https://example.com/?token=secret')])

    def test_unregistered_conflicting_mine_alias_cannot_look_clean(self):
        result=CleanupPolicy().review('s',[alias(1,'mine'),alias(2,'other-agent')],args())
        self.assertEqual(result['decision'],'block')

    def test_owned_retention_uses_physical_url_instead_of_stale_alias(self):
        p=CleanupPolicy(); pages=[alias(1,'mine')]
        self.review(p,pages,'use',pageIds=[1],reason='Actual native editor document work')
        self.review(p,pages,'retain',pageIds=[1],reason='Unsaved actual document still needed')
        result=self.review(p,pages,tabs=[tab(url='https://example.com/changed')])
        self.assertEqual(result['pendingPageIds'],[1])
        self.assertEqual(result['decision'],'block')
