"""Exercise one isolated Neo runtime with the actual stdio bridge and editor."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import urlsplit

import httpx
import psutil


class Client:
    def __init__(self, runtime, env, client):
        self.runtime, self.env, self.client = runtime, env, client
        self.task_id = "00000000-0000-4000-8000-000000000081"
        self.replies = queue.Queue()
        self.page_args = {}
        self.sequence = 0

    def __enter__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", str(self.runtime / "neo_mcp_bridge.py"),
             "--client", self.client, "--task-id", self.task_id],
            cwd=self.runtime, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        def read():
            for line in self.process.stdout:
                self.replies.put(line)
            self.replies.put(None)
        threading.Thread(target=read, daemon=True).start()
        try:
            self.call("initialize", protocolVersion="2025-06-18", capabilities={},
                      clientInfo={"name": "isolated-neo-smoke", "version": "1"})
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc):
        self.process.stdin.close()
        try:
            code = self.process.wait(timeout=15)
            if code and not exc[0]:
                raise RuntimeError(f"Bridge exited with {code}")
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=10)
            self.process.stdout.close()

    def call(self, method, **params):
        self.sequence += 1
        request = {"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params}
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        while True:
            raw = self.replies.get(timeout=75)
            if raw is None:
                raise RuntimeError("Stdio bridge closed while a reply was pending")
            reply = json.loads(raw)
            if reply.get("id") == self.sequence:
                if reply.get("error") or reply["result"].get("isError"):
                    raise RuntimeError(str(reply))
                return reply["result"]

    def tool(self, name, **args):
        return self.call("tools/call", name=name, arguments=args)

    def page(self, url):
        result = self.tool("tabs", action="list")
        body = "\n".join(item["text"] for item in result["content"] if item.get("type") == "text")
        for key, pattern in (("session", r"browseros-neo session:\s+([0-9a-f-]+)"),
                             ("inventory_receipt", r"inventory_receipt:\s+([0-9a-f-]+)")):
            if match := re.search(pattern, body):
                self.page_args[key] = match[1]
        ids = re.findall(r"^\[(\d+)\] " + re.escape(url) + r"\s", body, re.M)
        if len(ids) > 1:
            raise RuntimeError("The fixture opened duplicate pages")
        return int(ids[0]) if ids else None

    def run(self, page_id, code):
        out = self.tool("run", code=f"const p=browser.page({page_id}); {code}", **self.page_args)
        return json.dumps(out)

    def cleanup(self, action, **args):
        reply = self.tool("workspace_cleanup", action=action, task_id=self.task_id, **args)
        return reply.get("structuredContent") or json.loads(reply["content"][0]["text"])


def exercise(runtime, plan, client_kind, fixture_root, env):
    sys.path.insert(0, str(runtime))
    from web_automation.fixture import Fixture

    proxy = urlsplit(plan["mcp_endpoint"])
    ports = {proxy.port, int(plan["cdp_port"]), int(plan["backend_port"])}
    states = [Path(plan[name]).resolve() for name in ("profile_root", "server_state_root", "ledger_root")]
    if (proxy.hostname != "127.0.0.1" or len(ports) != 3 or ports & {9010, 9111, 9210}
        or len(set(states)) != 3 or any(not state.is_relative_to(runtime.parent) for state in states)):
        raise ValueError("Use a task-owned isolated runtime with separate local state and unused nonproduction ports.")

    def listeners():
        return {conn.laddr.port: conn.pid for conn in psutil.net_connections(kind="tcp")
                if conn.status == psutil.CONN_LISTEN and conn.laddr.port in ports}

    def command(args, timeout):
        result = subprocess.run(args, cwd=runtime, env=env, capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace")
                               + result.stdout.decode("utf-8", errors="replace"))
        return result.stdout

    def start():
        command(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(runtime / "start-neo.ps1"), "-Background"], 95)

    def stop():
        status = json.loads(command([sys.executable, "-B", "-X", "utf8",
                                     "stop-neo.py", "--confirmed-safe"], 48))
        if not status["browserClosed"] or status["browserExitCode"] or status["serverExitCode"] or listeners():
            raise RuntimeError(f"Isolated browser did not stop cleanly: {status}")
        return status

    def diagnose():
        return json.loads(command(["powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(runtime / "diagnose-neo.ps1")], 20))

    def session_counts():
        path = Path(plan["server_state_root"]) / "browserclaw.sqlite"
        with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
            return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("agent_session_starts", "agent_session_ends"))

    def target(http, url):
        response = http.get(f"http://127.0.0.1:{plan['cdp_port']}/json/list")
        response.raise_for_status()
        matches = [item["id"] for item in response.json() if item["url"] == url and item["type"] == "page"]
        if len(matches) != 1:
            raise RuntimeError(f"Expected one physical fixture tab, found {len(matches)}")
        return matches[0]

    if listeners() or fixture_root.exists() or (states[0] / "Local State").exists():
        raise ValueError("Use fresh fixture state, profile, and isolated ports.")
    server = Fixture(fixture_root).server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    read_draft = "return (await p.evaluate(()=>document.querySelector('#title').value)).value;"
    evidence = {}
    try:
        closed = diagnose()
        if closed["connectionCode"] != "MCP_NOT_LISTENING" or closed["mcpProbeRequested"] or listeners():
            raise RuntimeError(f"Unexpected diagnostic for stopped instance: {closed}")
        start()
        counts = session_counts()
        running = diagnose()
        if (running["connectionCode"] != "MCP_LISTENER_PRESENT_NOT_PROBED"
            or running["mcpProbeRequested"] or running["mcpReady"] is not None or session_counts() != counts):
            raise RuntimeError(f"Local diagnosis changed MCP session state or probed readiness: {running}")
        evidence["localDiagnosticsPreservedSessions"] = True
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as http:
            with Client(runtime, env, client_kind) as client:
                if client.page(url) is not None:
                    raise RuntimeError("Start the smoke test with a fresh profile.")
                client.tool("tabs", action="new", url=url, **client.page_args)
                page = client.page(url)
                client.run(page, "await p.waitForSelector('#title'); await p.evaluate(()=>{"
                           "const title=document.querySelector('#title'); title.value='neo-saved-draft';"
                           "document.querySelector('#rich').innerText='Synthetic body';"
                           "title.dispatchEvent(new Event('input',{bubbles:true}));"
                           "});")
                original = target(http, url)
                draft_url = url + "api/drafts/synthetic-demo"
                if http.get(draft_url).status_code != 404 or "neo-saved-draft" not in client.run(page, read_draft):
                    raise RuntimeError("No unsaved draft to preserve")
                # A maintenance caller holds a restart while current input is dirty.
                # Leaving the stdio connection simulates a host reconnect during that hold.
                before_reconnect = listeners()
            with Client(runtime, env, client_kind) as client:
                page = client.page(url)
                if target(http, url) != original or listeners() != before_reconnect:
                    raise RuntimeError("Reconnect replaced a tab or browser process")
                if "neo-saved-draft" not in client.run(page, read_draft) or http.get(draft_url).status_code != 404:
                    raise RuntimeError("Reconnect lost the unsaved draft")
                evidence.update(unsavedDraftPreserved=True, samePhysicalTargetAfterReconnect=True)
                saved = client.run(page, r"""const s=await p.snapshot();
                    const m=s.text.match(/button "Save draft".*?\[ref=(e\d+)\]/);
                    if(!m)throw new Error("Save control unavailable");
                    await p.click(m[1]); return await p.waitForText("Draft saved");""")
                if not re.search(r'"matched"\s*:\s*true', saved.replace(r'\"', '"')):
                    raise RuntimeError(f"Editor did not confirm saving: {saved}")
                response = http.get(draft_url)
                response.raise_for_status()
                if response.json()["title"] != "neo-saved-draft":
                    raise RuntimeError("Saved draft did not match")
                evidence["savedDraftReadback"] = True
            evidence["firstStop"] = stop()
            start()
            with Client(runtime, env, client_kind) as client:
                page = client.page(url)
                if page is None:
                    raise RuntimeError("The fixture did not restore")
                restored = target(http, url)
                preview = client.cleanup("resume")
                if "Explicit human designation required" in preview.get("reason", ""):
                    prior, = json.loads(preview["reason"].split("candidates: ", 1)[1])
                    candidate, = prior["candidates"]
                    if (candidate["blockedByOwner"] or len(candidate["pages"]) != 1
                        or candidate["pages"][0] != {"url": url, "targetId": restored}):
                        raise RuntimeError("Unexpected restoration candidates")
                    client.cleanup("resume", recovery={"prior_group_id": prior["priorGroupId"],
                        "group_id": candidate["groupId"], "pages": candidate["pages"],
                        "reason": "This invocation owns the exact isolated synthetic fixture group."})
                page = client.page(url)
                client.run(page, r"""await p.waitForSelector('#reopen');
                    const s=await p.snapshot();
                    const m=s.text.match(/button "Reopen saved draft".*?\[ref=(e\d+)\]/);
                    if(!m)throw new Error("Reopen control unavailable");
                    await p.click(m[1]); return await p.waitForText("Saved draft reopened");""")
                if ("neo-saved-draft" not in client.run(page, read_draft)
                    or response.json()["title"] != http.get(draft_url).json()["title"]):
                    raise RuntimeError("Saved draft did not reopen after browser restart")
                client.tool("tabs", action="close", page=page, **client.page_args)
                if client.page(url) is not None or any(
                    item["id"] == restored for item in http.get(f"http://127.0.0.1:{plan['cdp_port']}/json/list").json()
                ):
                    raise RuntimeError("The owned fixture tab is still present")
                check = client.cleanup("check")
                if check.get("decision") == "block" or check.get("cleanupVerified") is False:
                    raise RuntimeError(f"Cleanup still pending: {check}")
                evidence.update(reusedRestoredTab=True, ownedTabClosed=True)
            evidence["finalStop"] = stop()
            if evidence["firstStop"]["browserPid"] == evidence["finalStop"]["browserPid"]:
                raise RuntimeError("No actual browser process replacement observed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if listeners():
            stop()
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--client", choices=("codex", "agy"), required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    plan = json.loads((runtime / "runtime-plan.json").read_text(encoding="utf-8"))
    scratch = Path(plan["scratch_root"]).resolve(strict=True)
    if not scratch.is_relative_to(runtime.parent):
        raise ValueError("The isolated scratch must be inside the runtime's parent.")
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {"PSMODULEPATH", "PYTHONPATH", "PYTHONHOME", "CODEX_THREAD_ID",
                                  "WORKSPACE_NEO_TASK_ID", "WORKSPACE_NEO_EXECUTION_ID"}}
    env.update({key: str(scratch) for key in ("TEMP", "TMP", "TMPDIR")})
    evidence = exercise(runtime, plan, args.client, scratch / "smoke-fixture", env)
    print(json.dumps(evidence))
