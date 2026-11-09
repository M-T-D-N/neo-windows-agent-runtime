import unittest
from neo_cleanup import CleanupPolicy

TASK = "00000000-0000-4000-8000-000000000001"
def page(pid, target, url="https://example.com/", ownership="mine"):
    return dict(pageId=pid, targetId=target, url=url, ownership=ownership)

def args(action="check", **kw):
    return dict(action=action, task_id=TASK, **kw)

class CleanupTests(unittest.TestCase):
    def test_owned_physical_tabs_block_once_others_are_not_candidates(self):
        policy=CleanupPolicy()
        pages=[page(1,"a"),page(2,"a"),page(3,"b",ownership="user"),page(4,"c",ownership="other-agent")]
        result=policy.review("session",pages,args())
        self.assertEqual(result["pendingPageIds"],[2])
        self.assertEqual(result["decision"],"block")
        again=policy.review("session",pages,args(stop_hook_active=True))
        self.assertNotIn("decision",again)
        self.assertIn("systemMessage",again)

    def test_retention_exact_session_target_url_and_release(self):
        policy=CleanupPolicy(); pages=[page(1,"a")]
        keep=policy.review("s",pages,args("retain",pageIds=[1],reason="Saved editor required for approved next step"))
        self.assertEqual(keep["pendingPageIds"],[])
        self.assertNotIn("decision",policy.review("s",pages,args()))
        self.assertEqual(policy.review("other-session",pages,args())["decision"],"block")
        self.assertEqual(policy.review("s",[page(1,"new-target")],args())["decision"],"block")
        policy.review("s",pages,args("retain",pageIds=[1],reason="Confirmed next use"))
        self.assertEqual(policy.review("s",[page(1,"a","https://example.com/new")],args())["decision"],"block")
        policy.review("s",pages,args("retain",pageIds=[1],reason="Confirmed next use"))
        self.assertEqual(policy.review("s",pages,args("release",pageIds=[1]))["pendingPageIds"],[1])

    def test_retention_must_be_reaffirmed_after_accepted_stop(self):
        policy=CleanupPolicy(); pages=[page(1,"a")]
        policy.review("s",pages,args("retain",pageIds=[1],reason="Reader required for approved follow-up"))
        self.assertNotIn("decision",policy.review("s",pages,args()))
        first=policy.review("s",pages,args(stop_hook_active=False))
        self.assertNotIn("decision",first)
        next_boundary=policy.review("s",pages,args(stop_hook_active=False))
        self.assertEqual(next_boundary["reviewPageIds"],[1])
        self.assertEqual(next_boundary["pendingPageIds"],[])
        self.assertEqual(next_boundary["decision"],"block")
        followup=policy.review("s",pages,args(stop_hook_active=True))
        self.assertNotIn("decision",followup)
        self.assertIn("systemMessage",followup)
        self.assertEqual(followup["reviewPageIds"],[1])
        policy.review("s",pages,args("retain",pageIds=[1],reason="Approved follow-up is still in progress"))
        self.assertNotIn("decision",policy.review("s",pages,args(stop_hook_active=True)))
        self.assertEqual(policy.review("s",pages,args())["reviewPageIds"],[1])
        released=policy.review("s",pages,args("release",pageIds=[1]))
        self.assertEqual(released["pendingPageIds"],[1])
        self.assertEqual(released.get("reviewPageIds",[]),[])
        self.assertEqual(policy.review("s",[],args(stop_hook_active=False))["pendingPageIds"],[])

    def test_cleanup_of_disposable_page_does_not_expire_current_retention(self):
        policy=CleanupPolicy(); pages=[page(1,"a"),page(2,"b")]
        policy.review("s",pages,args("retain",pageIds=[1],reason="Unsaved editor waiting for user input"))
        blocked=policy.review("s",pages,args(stop_hook_active=False))
        self.assertEqual(blocked["pendingPageIds"],[2])
        self.assertEqual(blocked.get("reviewPageIds",[]),[])
        done=policy.review("s",[pages[0]],args(stop_hook_active=True))
        self.assertNotIn("decision",done)
        self.assertNotIn("systemMessage",done)
        next_step=policy.review("s",[pages[0]],args(stop_hook_active=False))
        self.assertEqual(next_step["reviewPageIds"],[1])
        self.assertIn("Unsaved editor",next_step["retained"][0]["reason"])

    def test_cannot_retain_other_task_or_bind_another_thread(self):
        policy=CleanupPolicy()
        with self.assertRaises(ValueError):
            policy.review("s",[page(1,"a",ownership="other-agent")],args("retain",pageIds=[1],reason="Retain someone else"))
        with self.assertRaises(ValueError):
            policy.review("s",[],dict(action="check",task_id="00000000-0000-4000-8000-000000000002"))

    def test_missing_identity_or_listing_never_passes(self):
        for pages in (None,[dict(pageId=1,url="about:blank",ownership="mine")]):
            with self.assertRaises(ValueError):
                CleanupPolicy().review("s",pages,args())

    def test_closed_tabs_release_retention_record(self):
        policy=CleanupPolicy()
        policy.review("s",[page(1,"a")],args("retain",pageIds=[1],reason="Confirmed next use"))
        result=policy.review("s",[],args())
        self.assertEqual(result["pendingPageIds"],[])
        self.assertEqual(policy.retained,{})


class RetiredTargetTests(unittest.TestCase):
    def test_closed_prior_session_retention_is_pruned_but_live_other_target_is_preserved(self):
        policy=CleanupPolicy()
        task='00000000-0000-4000-8000-000000000001'
        old=('prior','old-target','https://example.com/a')
        live=('prior','live-target','https://example.com/b')
        policy.retained={old:'Previous restart test',live:'Other task pending input'}
        result=policy.review('current',[dict(pageId=4,targetId='live-target',url='https://example.com/b',ownership='other-agent')],dict(action='check',task_id=task))
        self.assertEqual(result,{'pendingPageIds':[],'retained':[]})
        self.assertEqual(policy.retained,{live:'Other task pending input'})
