"""Local fictional editor server. It deliberately provides no publish idempotency."""
import base64
import binascii
import hashlib
import json
import re
import socket
import threading
import time
import uuid
from html import escape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from workspace_storage import atomic_write_bytes, atomic_write_json
from .ledger import Ledger, LedgerError, Blocked, slug, text
from .storage import FileLock

MAX_REQUEST = 7 * 1024 * 1024
MAX_UPLOAD = 4 * 1024 * 1024
STATIC = Path(__file__).with_name("static")
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class RichHTML(HTMLParser):
    def __init__(self, uploads):
        super().__init__(convert_charrefs=True)
        self.uploads, self.output = uploads, []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "strong", "b", "em", "i", "ul", "ol", "li", "br"}:
            self.output.append(f"<{tag}>")
        elif tag == "a":
            href = dict(attrs).get("href", "")
            if urlsplit(href).scheme in {"http", "https"}:
                self.output.append(f'<a href="{escape(href, quote=True)}" rel="noopener noreferrer">')
        elif tag == "img":
            src = dict(attrs).get("src", "")
            match = re.fullmatch(r"/api/uploads/([a-f0-9]{32})", src)
            if match and match[1] in self.uploads and self.uploads[match[1]]["type"] in IMAGE_TYPES:
                self.output.append(f'<img src="{src}" alt="{escape(dict(attrs).get("alt", ""), quote=True)}">')

    def handle_endtag(self, tag):
        if tag in {"p", "div", "strong", "b", "em", "i", "ul", "ol", "li", "a"}:
            self.output.append(f"</{tag}>")

    def handle_data(self, value):
        self.output.append(escape(value))


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("fixture state root must be absolute")
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "fixture.json"
        self.transaction = self.root / "fixture.transaction"
        self.guard = Ledger(self.root / "guard-ledger.json")
        self.thread_lock = threading.RLock()
        self.threads = []

    def read(self):
        if not self.path.exists():
            return {"schema_version": 1, "drafts": {}, "posts": {}, "uploads": {}, "submission_count": 0, "intents": {}}
        with self.path.open(encoding="utf-8-sig") as stream:
            state = json.load(stream)
        if state.get("schema_version") != 1 or set(state) != {"schema_version", "drafts", "posts", "uploads", "submission_count", "intents"}:
            raise ValueError("unreadable fixture state; preserve and inspect")
        return state

    def write(self, state):
        atomic_write_json(self.path, state, backup_count=3, require_schema_version=True)

    def content(self, data, state):
        operation = slug(data.get("operation_id"))
        title = text(data.get("title"), "title", 200)
        body = data.get("body_html", "")
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            raise ValueError("a nonempty body_html of at most 100000 characters is required")
        ids = data.get("attachments", [])
        if not isinstance(ids, list) or len(ids) > 16 or any(not isinstance(item, str) or item not in state["uploads"] for item in ids) or len(set(ids)) != len(ids):
            raise ValueError("invalid ordered attachment IDs")
        sanitizer = RichHTML(state["uploads"])
        sanitizer.feed(body)
        sanitized = "".join(sanitizer.output)
        plain = re.sub(r"<[^>]+>", "", sanitized).strip()
        if not plain and "<img " not in sanitized:
            raise ValueError("body must contain text or an uploaded image")
        return {"operation_id": operation, "title": title, "body_html": sanitized, "plain_body": data.get("plain_body", "")[:100000] if isinstance(data.get("plain_body", ""), str) else "", "attachments": [state["uploads"][item] for item in ids]}

    def server(self, port=0):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, payload, mime="application/json; charset=utf-8"):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if not isinstance(payload, bytes) else payload
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(raw)

            def allowed(self, mutation=False):
                host = f"127.0.0.1:{self.server.server_port}"
                if self.headers.get("Host") != host:
                    self.respond(403, {"error": "exact loopback Host required"})
                    return False
                origin = self.headers.get("Origin")
                if origin is not None and origin != "http://" + host:
                    self.respond(403, {"error": "cross-origin request refused"})
                    return False
                if mutation and self.headers.get("X-Fixture-Request") != "1":
                    self.respond(403, {"error": "fixture request header required"})
                    return False
                return True

            def do_GET(self):
                if not self.allowed():
                    return
                parsed = urlsplit(self.path)
                assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/editor.html": ("editor.html", "text/html; charset=utf-8")}
                if parsed.path in assets:
                    filename, mime = assets[parsed.path]
                    return self.respond(200, (STATIC / filename).read_bytes(), mime)
                with fixture.thread_lock, FileLock(fixture.transaction):
                    state = fixture.read()
                    if parsed.path.startswith("/api/guard/"):
                        operation_id = parsed.path[len("/api/guard/"):]
                        try:
                            slug(operation_id)
                            operation = fixture.guard.status()["operations"].get(operation_id)
                            return self.respond(200, {"operation": operation, "expected_content": state["intents"].get(operation_id)})
                        except (LedgerError, OSError) as exc:
                            return self.respond(409, {"error": str(exc)})
                    if parsed.path == "/api/posts":
                        operation = parse_qs(parsed.query).get("operation_id", [None])[0]
                        posts = [p for p in state["posts"].values() if operation is None or p["operation_id"] == operation]
                        return self.respond(200, {"posts": posts, "submission_count": state["submission_count"]})
                    for prefix, kind in (("/api/drafts/", "drafts"), ("/api/posts/", "posts"), ("/api/uploads/", "uploads")):
                        if parsed.path.startswith(prefix):
                            key = parsed.path[len(prefix):]
                            found = state[kind].get(key)
                            if found is None:
                                return self.respond(404, {"error": "not found"})
                            if kind == "uploads":
                                mime = found["type"] if found["type"] in IMAGE_TYPES else "application/octet-stream"
                                return self.respond(200, (fixture.root / "uploads" / key).read_bytes(), mime)
                            return self.respond(200, found)
                self.respond(404, {"error": "not found"})

            def guard_begin(self, data, state):
                content = fixture.content(data, state)
                operation_id = content["operation_id"]
                fingerprint = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                fixture.guard.prepare(operation_id, domain="127.0.0.1", account="synthetic-fixture-account", channel="synthetic-fixture-board", manuscript_version=fingerprint, attachments=[item["id"] + ":" + item["sha256"] for item in content["attachments"]], approval_reference="fictional-fixture-request:" + operation_id)
                if operation_id not in state["intents"]:
                    state["intents"][operation_id] = content
                    fixture.write(state)
                elif state["intents"][operation_id] != content:
                    raise LedgerError("fixture content intent is immutable")
                operation = fixture.guard.begin(operation_id)
                return self.respond(200, {"operation": operation, "expected_content": content})

            def guard_reconcile(self, data, state):
                operation_id = slug(data.get("operation_id"))
                matching = [post for post in state["posts"].values() if post["operation_id"] == operation_id]
                if len(matching) != 1 or matching[0]["post_id"] != data.get("post_id"):
                    raise LedgerError("exactly one matching saved post is required; zero or duplicates cannot release the guard")
                post = matching[0]
                expected = state["intents"].get(operation_id)
                if expected is None or any(post.get(key) != value for key, value in expected.items()):
                    raise LedgerError("saved post title/body/ordered attachment content does not match the immutable intent")
                fingerprint = hashlib.sha256(json.dumps(expected, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                operation = fixture.guard.reconcile(operation_id, "VERIFIED", post_id=post["post_id"], url=post["url"], verification={"kind": "post-readback", "reference": "fixture-post-readback:" + post["post_id"] + ":sha256:" + fingerprint})
                return self.respond(200, {"operation": operation, "content": post})

            def upload(self, data, state):
                name = text(data.get("name"), "file name", 120)
                if re.search(r'[\\/:*?"<>|]', name) or name in {".", ".."} or name.endswith((".", " ")):
                    raise ValueError("unsafe upload name")
                mime = text(data.get("type", "application/octet-stream"), "file type", 100)
                raw = base64.b64decode(data.get("base64", ""), validate=True)
                if not 0 < len(raw) <= MAX_UPLOAD:
                    raise ValueError("upload must contain 1 byte to 4 MiB")
                key = uuid.uuid4().hex
                metadata = {"id": key, "name": name, "type": mime, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                atomic_write_bytes(fixture.root / "uploads" / key, raw)
                state["uploads"][key] = metadata
                fixture.write(state)
                return self.respond(201, metadata)

            def save_content(self, data, state):
                content = fixture.content(data, state)
                if self.path == "/api/draft":
                    state["drafts"][content["operation_id"]] = content
                    fixture.write(state)
                    return self.respond(200, {"status": "draft-saved", "content": content})
                state["submission_count"] += 1
                content["post_id"] = uuid.uuid4().hex
                content["url"] = f"http://127.0.0.1:{self.server.server_port}/api/posts/{content['post_id']}"
                state["posts"][content["post_id"]] = content
                fixture.write(state)
                if data.get("drop_after_commit"):
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                return self.respond(201, {"status": "published", "content": content})

            def do_POST(self):
                if not self.allowed(mutation=True):
                    return
                try:
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise ValueError("application/json required")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_REQUEST:
                        raise ValueError("request size outside fixture bounds")
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError("JSON object required")
                    delay = data.get("delay_ms", 0)
                    if type(delay) is not int or not 0 <= delay <= 3000:
                        raise ValueError("delay_ms must be between 0 and 3000")
                    for knob in ("fail", "drop_after_commit"):
                        if knob in data and type(data[knob]) is not bool:
                            raise ValueError(f"{knob} must be boolean")
                    if delay:
                        time.sleep(delay / 1000)
                    if data.get("fail"):
                        return self.respond(503, {"error": "synthetic failure before commit"})
                    with fixture.thread_lock, FileLock(fixture.transaction):
                        state = fixture.read()
                        if self.path == "/api/guard/begin":
                            return self.guard_begin(data, state)
                        if self.path == "/api/guard/uncertain":
                            operation = fixture.guard.uncertain(slug(data.get("operation_id")), text(data.get("reference"), "uncertainty reference"))
                            return self.respond(200, {"operation": operation})
                        if self.path == "/api/guard/reconcile":
                            return self.guard_reconcile(data, state)
                        if self.path == "/api/upload":
                            return self.upload(data, state)
                        if self.path in {"/api/draft", "/api/publish"}:
                            return self.save_content(data, state)
                    self.respond(404, {"error": "not found"})
                except Blocked as exc:
                    self.respond(409, {"error": str(exc), "operation": exc.operation})
                except LedgerError as exc:
                    self.respond(409, {"error": str(exc)})
                except (ValueError, TypeError, binascii.Error) as exc:
                    self.respond(400, {"error": str(exc)})
                except OSError as exc:
                    self.respond(500, {"error": f"storage failure; inspect run state before retry: {exc}"})

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = False
        server.block_on_close = True
        return server
