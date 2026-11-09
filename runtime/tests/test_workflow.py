import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from workspace_storage import StorageVersionError, StorageWriteError
from web_automation.fixture import Fixture
from web_automation.ledger import Ledger, Blocked, LedgerError
from web_automation.__main__ import main

IDENTITY = dict(domain="127.0.0.1", account="fictional-account", channel="fictional-board", manuscript_version="synthetic-v1", attachments=[], approval_reference="request:synthetic-test")


class RootTest(unittest.TestCase):
    def setUp(self):
        root = os.environ.get("WEB_AUTOMATION_TEST_ROOT")
        if not root or not Path(root).is_absolute():
            raise RuntimeError("WEB_AUTOMATION_TEST_ROOT must be an explicit absolute installed scratch path")
        root_path = Path(root)
        from workspace_storage import ensure_no_reparse_components
        ensure_no_reparse_components(root_path)
        allowed = Path(json.loads((Path(__file__).parents[1]/'runtime-plan.json').read_text(encoding='utf-8'))['scratch_root']).resolve()
        if not root_path.resolve().is_relative_to(allowed):
            raise RuntimeError("test root must be an explicitly owned installed scratch task path")
        root_path.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="test-", dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = Ledger(self.root / "ledger.json")

    def prepare(self, op="test-op", **changes):
        return self.ledger.prepare(op, **(IDENTITY | changes))


class LedgerTests(RootTest):
    def test_restart_prevents_replay_and_verified_returns_existing_result(self):
        self.prepare()
        self.ledger.begin("test-op")
        restarted = Ledger(self.ledger.path)
        with self.assertRaises(Blocked):
            restarted.begin("test-op")
        self.assertEqual(restarted.status("test-op")["status"], "NEEDS_RECONCILIATION")
        verified = restarted.reconcile("test-op", "VERIFIED", post_id="post-1", url="http://127.0.0.1:8000/api/posts/post-1", verification={"kind": "post-readback", "reference": "saved-content-hash:abc"})
        self.assertEqual(restarted.begin("test-op"), verified)
        self.assertEqual(self.prepare(), verified)
        with self.assertRaises(Blocked):
            self.prepare(manuscript_version="synthetic-v2")

    def test_same_account_busy_across_channels_and_other_account_independent(self):
        self.prepare("first")
        self.prepare("second", channel="other-board")
        self.prepare("third", account="another-fictional-account")
        self.ledger.begin("first")
        with self.assertRaises(Blocked):
            Ledger(self.ledger.path).begin("second")
        self.assertEqual(self.ledger.begin("third")["status"], "SUBMITTING")

    def test_positive_absence_required_no_automatic_retry(self):
        self.prepare()
        self.ledger.begin("test-op")
        self.ledger.uncertain("test-op", "connection dropped")
        with self.assertRaises(LedgerError):
            self.ledger.reconcile("test-op", "FAILED_CONFIRMED", verification={"kind": "list-miss", "reference": "first page did not contain post"})
        absent = self.ledger.reconcile("test-op", "FAILED_CONFIRMED", verification={"kind": "publisher-confirmed-absence", "reference": "authoritative publisher rejected-before-commit evidence"})
        self.assertEqual(absent["status"], "FAILED_CONFIRMED")
        with self.assertRaises(Blocked):
            self.ledger.begin("test-op")

    def test_invalid_and_future_schema_preserved(self):
        for raw in (b"not json", b'{"schema_version":2,"operations":{}}', b'{"schema_version":1,"operations":{"x":{}}}'):
            self.ledger.path.write_bytes(raw)
            with self.assertRaises((LedgerError, StorageVersionError)):
                self.prepare()
            self.assertEqual(self.ledger.path.read_bytes(), raw)

    def test_missing_primary_existing_backup_is_not_initialized(self):
        self.ledger.path.with_name("ledger.json.bak.1").write_text('{"schema_version":1,"operations":{}}', encoding="utf-8")
        with self.assertRaises(LedgerError):
            self.prepare()
        self.assertFalse(self.ledger.path.exists())

    def test_submitting_is_persisted_before_permission_to_submit(self):
        self.prepare()
        returned = self.ledger.begin("test-op")
        saved = json.loads(self.ledger.path.read_text(encoding="utf-8"))
        self.assertEqual(returned["status"], "SUBMITTING")
        self.assertEqual(saved["operations"]["test-op"]["status"], "SUBMITTING")
        self.assertTrue(self.ledger.path.with_name("ledger.json.bak.1").exists())

    def test_atomic_error_after_final_install_fails_closed(self):
        self.prepare()
        from web_automation import ledger as module
        original = module.atomic_write_json
        def fault(*args, **kwargs):
            original(*args, **kwargs)
            raise StorageWriteError("synthetic backup failure", final_installed=True, recovery_paths=(self.root / "staged",))
        with patch.object(module, "atomic_write_json", fault):
            with self.assertRaisesRegex(LedgerError, "final_installed=True"):
                self.ledger.begin("test-op")
        self.assertEqual(self.ledger.status("test-op")["status"], "SUBMITTING")
        with self.assertRaises(Blocked):
            self.ledger.begin("test-op")

    def test_simultaneous_account_begin_has_one_winner(self):
        self.prepare("first")
        self.prepare("second", channel="other-board")
        barrier = threading.Barrier(2)
        outputs = []
        def begin(op):
            barrier.wait()
            try:
                outputs.append(Ledger(self.ledger.path).begin(op)["status"])
            except Blocked:
                outputs.append("BLOCKED")
        threads = [threading.Thread(target=begin, args=(op,)) for op in ("first", "second")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(outputs, ["SUBMITTING", "BLOCKED"])

    def test_operation_and_url_validation(self):
        for op in ("../escape", "Uppercase", "", "x" * 81):
            with self.assertRaises(LedgerError):
                self.prepare(op)
        self.prepare()
        self.ledger.begin("test-op")
        with self.assertRaises(LedgerError):
            self.ledger.reconcile("test-op", "VERIFIED", post_id="x", url="https://external.invalid/post/x", verification={"kind": "post-readback", "reference": "evidence"})
        self.assertEqual(self.ledger.status("test-op")["status"], "SUBMITTING")

    def test_cli_observable_nonzero_blocked(self):
        args = ["--storage-root", str(self.root / "cli")]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(args + ["prepare", "cli-op", "--domain", "127.0.0.1", "--account", "fictional", "--channel", "board", "--manuscript-version", "v1", "--approval-reference", "request:test"]), 0)
            self.assertEqual(main(args + ["begin", "cli-op"]), 0)
            self.assertEqual(main(args + ["begin", "cli-op"]), 3)
            self.assertEqual(main(args + ["status", "cli-op"]), 0)
        self.assertIn('"blocked"', out.getvalue())
        self.assertIn("NEEDS_RECONCILIATION", out.getvalue())


class FixtureTests(RootTest):
    def setUp(self):
        super().setUp()
        self.fixture = Fixture(self.root / "fixture")
        self.server = self.fixture.server()
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, data=None, headers=None):
        raw = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, raw, headers={"Content-Type": "application/json", "X-Fixture-Request": "1"} | (headers or {}))
        with urllib.request.urlopen(req, timeout=8) as response:
            payload = response.read()
            return json.loads(payload) if response.headers.get_content_type() == "application/json" else payload

    def upload(self, name="synthetic.txt", raw="合成 첨부".encode(), **knobs):
        return self.request("/api/upload", {"name": name, "type": "text/plain", "base64": base64.b64encode(raw).decode()} | knobs)

    def content(self, attachments=None, **knobs):
        return {"operation_id": "synthetic-op", "title": "合成 시험 제목", "body_html": '<p>한글 <strong>굵게</strong> <a href="https://example.test/path">링크</a></p>', "plain_body": "한글 굵게 링크", "attachments": attachments or []} | knobs

    def test_saved_unicode_rich_content_order_hash_and_publish_readback(self):
        first = self.upload()
        second = self.upload("second.txt", b"second-content")
        content = self.content([second["id"], first["id"]])
        self.request("/api/draft", content)
        reopened = self.request("/api/drafts/synthetic-op")
        self.assertEqual(reopened["title"], "合成 시험 제목")
        self.assertIn("<strong>굵게</strong>", reopened["body_html"])
        self.assertIn('href="https://example.test/path"', reopened["body_html"])
        self.assertEqual([item["id"] for item in reopened["attachments"]], content["attachments"])
        self.assertEqual(reopened["attachments"][1]["sha256"], hashlib.sha256("合成 첨부".encode()).hexdigest())
        self.assertEqual(self.request("/api/uploads/" + first["id"]), "合成 첨부".encode())
        post = self.request("/api/publish", content)["content"]
        readback = self.request("/api/posts/" + post["post_id"])
        self.assertEqual(readback["body_html"], reopened["body_html"])
        self.assertEqual(readback["attachments"], reopened["attachments"])
        self.assertEqual(self.request("/api/posts?operation_id=synthetic-op")["submission_count"], 1)

    def test_failed_and_delayed_upload(self):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.upload(fail=True)
        self.assertEqual(failure.exception.code, 503)
        self.assertEqual(self.fixture.read()["uploads"], {})
        started = time.monotonic()
        delayed = self.upload(delay_ms=150)
        self.assertGreaterEqual(time.monotonic() - started, .12)
        self.assertEqual(delayed["size"], len("合成 첨부".encode()))

    def test_drop_after_commit_reconciled_without_replay(self):
        self.prepare("synthetic-op")
        self.ledger.begin("synthetic-op")
        with self.assertRaises((RemoteDisconnected, urllib.error.URLError, ConnectionResetError)):
            self.request("/api/publish", self.content(drop_after_commit=True))
        self.ledger.uncertain("synthetic-op", "response dropped after synthetic publish")
        with self.assertRaises(Blocked):
            Ledger(self.ledger.path).begin("synthetic-op")
        listed = self.request("/api/posts?operation_id=synthetic-op")
        self.assertEqual(len(listed["posts"]), 1)
        post = listed["posts"][0]
        readback = self.request("/api/posts/" + post["post_id"])
        self.assertEqual(readback["title"], self.content()["title"])
        self.ledger.reconcile("synthetic-op", "VERIFIED", post_id=post["post_id"], url=post["url"], verification={"kind": "post-readback", "reference": "fixture post content and ordered attachment readback"})
        self.assertEqual(self.ledger.begin("synthetic-op")["result"]["post_id"], post["post_id"])
        self.assertEqual(self.request("/api/posts")["submission_count"], 1)

    def test_server_has_no_hidden_idempotency(self):
        self.request("/api/publish", self.content())
        self.request("/api/publish", self.content())
        value = self.request("/api/posts")
        self.assertEqual(value["submission_count"], 2)
        self.assertEqual(len(value["posts"]), 2)
        self.assertNotEqual(value["posts"][0]["post_id"], value["posts"][1]["post_id"])

    def test_upload_bounds_paths_origin_and_sanitization(self):
        for name in ("../escape", "C:\\escape", "x/escape"):
            with self.assertRaises(urllib.error.HTTPError):
                self.upload(name)
        with self.assertRaises(urllib.error.HTTPError) as forbidden:
            self.request("/api/publish", self.content(), {"Origin": "https://external.invalid"})
        self.assertEqual(forbidden.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/upload", {"name": "bad", "type": "text/plain", "base64": "!invalid!"})
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/publish", self.content(attachments=["unknown-id"]))
        value = self.request("/api/draft", self.content(body_html='<p onclick="bad()">text</p><script>bad()</script><a href="javascript:bad()">bad</a>'))
        self.assertNotIn("<script", value["content"]["body_html"])
        self.assertNotIn("onclick", value["content"]["body_html"])
        self.assertNotIn("javascript:", value["content"]["body_html"])
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/../../AGENTS.md")

    def test_image_bytes_preview_endpoint_and_inserted_structure(self):
        # Tiny fixed synthetic PNG bytes, not a user file.
        raw = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")
        image = self.request("/api/upload", {"name": "pixel.png", "type": "image/png", "base64": base64.b64encode(raw).decode()})
        body = f'<p>Image</p><img src="/api/uploads/{image["id"]}" alt="synthetic pixel">'
        post = self.request("/api/publish", self.content([image["id"]], body_html=body))["content"]
        self.assertIn(f'/api/uploads/{image["id"]}', post["body_html"])
        self.assertEqual(self.request("/api/uploads/" + image["id"]), raw)
        self.assertEqual(post["attachments"][0]["sha256"], hashlib.sha256(raw).hexdigest())


    def test_fixture_guard_survives_reload_and_reconciles_exact_saved_post(self):
        content = self.content()
        begun = self.request("/api/guard/begin", content)
        self.assertEqual(begun["operation"]["status"], "SUBMITTING")
        reopened = Fixture(self.fixture.root)
        self.assertEqual(reopened.guard.status("synthetic-op")["status"], "SUBMITTING")
        with self.assertRaises(urllib.error.HTTPError) as absent:
            self.request("/api/guard/reconcile", {"operation_id": "synthetic-op", "post_id": "not-yet-posted"})
        self.assertEqual(absent.exception.code, 409)
        self.assertEqual(self.request("/api/guard/synthetic-op")["operation"]["status"], "SUBMITTING")
        with self.assertRaises((RemoteDisconnected, urllib.error.URLError, ConnectionResetError)):
            self.request("/api/publish", content | {"drop_after_commit": True})
        self.request("/api/guard/uncertain", {"operation_id": "synthetic-op", "reference": "response-lost"})
        with self.assertRaises(urllib.error.HTTPError) as replay:
            self.request("/api/guard/begin", content)
        self.assertEqual(replay.exception.code, 409)
        listed = self.request("/api/posts?operation_id=synthetic-op")
        self.assertEqual(listed["submission_count"], 1)
        saved = self.request("/api/posts/" + listed["posts"][0]["post_id"])
        verified = self.request("/api/guard/reconcile", {"operation_id": "synthetic-op", "post_id": saved["post_id"]})
        self.assertEqual(verified["operation"]["status"], "VERIFIED")
        self.assertEqual(self.request("/api/guard/begin", content)["operation"]["status"], "VERIFIED")
        self.assertEqual(self.request("/api/posts")["submission_count"], 1)

    def test_fixture_guard_rejects_mismatched_saved_content_and_duplicates(self):
        self.request("/api/guard/begin", self.content())
        different = self.request("/api/publish", self.content(title="Different saved title"))["content"]
        with self.assertRaises(urllib.error.HTTPError) as mismatch:
            self.request("/api/guard/reconcile", {"operation_id": "synthetic-op", "post_id": different["post_id"]})
        self.assertEqual(mismatch.exception.code, 409)
        self.assertEqual(self.request("/api/guard/synthetic-op")["operation"]["status"], "SUBMITTING")
        duplicate = self.request("/api/publish", self.content())["content"]
        with self.assertRaises(urllib.error.HTTPError) as multiple:
            self.request("/api/guard/reconcile", {"operation_id": "synthetic-op", "post_id": duplicate["post_id"]})
        self.assertEqual(multiple.exception.code, 409)
        self.assertEqual(self.request("/api/posts")["submission_count"], 2)

    def test_fixture_guard_validates_slug_and_same_account_serialization(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/guard/begin", self.content(operation_id="../bad"))
        self.request("/api/guard/begin", self.content())
        with self.assertRaises(urllib.error.HTTPError) as busy:
            self.request("/api/guard/begin", self.content(operation_id="another-op"))
        self.assertEqual(busy.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/guard/begin", self.content(body_html="Changed intent"))
        self.assertEqual(self.request("/api/posts")["submission_count"], 0)

    def test_fixture_guard_compares_ordered_attachments_and_body(self):
        first, second = self.upload("first.txt", b"one"), self.upload("second.txt", b"two")
        expected = self.content([first["id"], second["id"]])
        self.request("/api/guard/begin", expected)
        wrong = self.request("/api/publish", self.content([second["id"], first["id"]], body_html="Wrong body"))["content"]
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/guard/reconcile", {"operation_id": "synthetic-op", "post_id": wrong["post_id"]})
        self.assertEqual(self.request("/api/guard/synthetic-op")["operation"]["status"], "SUBMITTING")
