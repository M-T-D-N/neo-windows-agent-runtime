"""Workspace-owned stdio transport for the approved BrowserOS neo HTTP MCP.

Discovers tools locally; starts the dedicated runtime only for browser work. Only initialization/readiness is
retried: a dispatched tool call is never replayed, including after response loss.
The official server owns all tool schemas, results, metadata and browser state.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

TOOL_PROTOCOL = "2026-07-28"
SESSION_META_KEY = "com.browseros.neo/session"

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from process_environment import build_environment
from workspace_storage import atomic_write_text
from neo_cleanup import CleanupPolicy, TOOL as CLEANUP_TOOL, TOOL_NAME as CLEANUP_TOOL_NAME, has_pending_cleanup
from neo_task_state import TaskState
from neo_tab_identity import credential_bearing_url


class UpstreamError(Exception):
    pass


class EndpointIdentityError(UpstreamError):
    pass


class NotReady(UpstreamError):
    pass


class SessionGone(UpstreamError):
    pass


if sys.platform == "win32":
    class GuardedProactorEventLoop(asyncio.ProactorEventLoop):
        """Stop this bridge if Windows' internal wakeup channel reaches EOF.

        CPython 3.13 re-arms this read even on EOF, spinning an otherwise idle
        process. Exit rather than retrying requests or waiting for blocked stdin
        executor shutdown. This hook is local to the bridge, not shared Python.
        """
        def _loop_self_reading(self, f=None):
            if (f is not None and f is self._self_reading_future
                    and f.done() and not f.cancelled()):
                try:
                    data = f.result()
                except Exception:
                    # Preserve CPython's existing error handling.
                    pass
                else:
                    if data == b"":
                        print("NEO_LOOP_CHANNEL_CLOSED: Windows internal socket reached EOF; "
                              "bridge exiting to prevent CPU spin. Dispatched actions "
                              "must be inspected before retry; nothing is replayed.",
                              file=sys.stderr, flush=True)
                        os._exit(75)
            super()._loop_self_reading(f)


async def sse_messages(response: httpx.Response):
    """Decode complete SSE data events; comments/empty keepalives are harmless."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                payload = "\n".join(data)
                data.clear()
                if payload.strip():
                    yield json.loads(payload)
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data and "\n".join(data).strip():
        yield json.loads("\n".join(data))


class Bridge:
    def __init__(self, endpoint: str, emit: Callable[[dict], None], *, client=None,
                 starter=None, startup_seconds: float = 45, catalog=None, state_root=None, task_id=None,
                 backend_resolver=None, endpoint_guard=None):
        address = urlsplit(endpoint)
        if (address.scheme != "http" or address.hostname != "127.0.0.1"
                or address.path != "/mcp" or address.username or address.password
                or address.query or address.fragment):
            raise ValueError("Only the approved loopback /mcp endpoint is supported")
        self.endpoint = endpoint
        self.canonical_endpoint = endpoint
        self.backend_resolver = backend_resolver
        self.endpoint_guard = endpoint_guard
        self.emit = emit
        self.client = client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(185, connect=2, write=15, pool=10))
        self.starter = starter or self.start_runtime
        self.startup_seconds = startup_seconds
        self.session: str | None = None
        self.protocol: str | None = None
        self.initialize: dict | None = None
        self.initialized = False
        self.lock = asyncio.Lock()
        self.event_task: asyncio.Task | None = None
        self.sequence = 0
        # Browser identity is independent of the disposable HTTP handshake.
        self.tool_session: str | None = None
        self.tool_lock = asyncio.Lock()
        self.active_tools: dict[Any, asyncio.Task] = {}
        self.cleanup = CleanupPolicy()
        self.cleanup_empty_session: str | None = None
        self.cleanup_closed_session: str | None = None
        self.catalog = catalog or json.loads((ROOT / 'official-tools.json').read_text(encoding='utf-8'))
        self.task_state = TaskState(state_root) if state_root else None
        self.task_prepared = False
        self.execution_id = os.environ.get('WORKSPACE_NEO_EXECUTION_ID')
        if task_id and self.task_state:
            self.bind_task(task_id)

    async def guard_endpoint(self):
        if self.endpoint_guard is not None:
            try:
                return await asyncio.to_thread(self.endpoint_guard, self.endpoint)
            except Exception as error:
                raise EndpointIdentityError('Endpoint ownership check failed before dispatch: '+str(error)) from error

    def bind_task(self, task_id):
        self.cleanup.bind(task_id)
        if self.task_state:
            was_bound = self.task_state.task_id is not None
            record = self.task_state.bind(task_id)
            if self.tool_session is None:
                self.tool_session = record['session']
            if record.get('browser_closed') and not record.get('pages') and not record.get('used_tabs'):
                self.cleanup_closed_session = self.tool_session
            if not was_bound:
                self.cleanup.retained = {tuple(k):v for k,v in record.get('retained',[])}
                self.cleanup.review_required = set(self.cleanup.retained)
                self.cleanup.used = {item['targetId']:item for item in record.get('used_tabs',[])}
                for item in self.cleanup.used.values():
                    if item.get('deferred'):
                        item['deferred']['review_required'] = True

    async def internal_tool(self, name, arguments):
        self.sequence += 1
        call = {'jsonrpc':'2.0', 'id':f'neo-task-{self.sequence}', 'method':'tools/call',
                'params':{'name':name, 'arguments':arguments}}
        request, headers = self.tool_request(call)
        reply = await self.post(request, headers=headers, timeout=20)
        if not reply or 'error' in reply or 'result' not in reply:
            raise UpstreamError('Task reconciliation RPC failed; preserve existing bindings')
        result = (reply or {}).get('result') or {}
        handle = (result.get('_meta') or {}).get(SESSION_META_KEY)
        if handle:
            self.tool_session = handle
        if result.get('isError'):
            raise UpstreamError('Task reconciliation failed; no browser action may be replayed')
        return result

    async def inventory(self):
        result = await self.internal_tool('run', {'code':
            'const pages=await browser.pages.list(); const targets=(await browser.cdp("Target.getTargets",{})).targetInfos.map(t=>t.targetId); '
            'const groups=(await browser.cdp("Browser.getTabGroups",{})).groups; '
            'return {pages:pages.filter(p=>!p.targetId || targets.includes(p.targetId)),groups};'})
        structured = result.get('structuredContent') or {}
        if structured.get('ok') is not True or not isinstance(structured.get('value'), dict):
            raise UpstreamError('Task inventory unavailable; preserve prior bindings')
        return structured['value']

    async def recovery_inventory(self, group_id):
        """Metadata only, including during conflicts; never reconcile or save bindings."""
        info = await self.inventory()  # Fixed internal read; no readiness startup.
        groups = [g for g in info['groups'] if g.get('id', g.get('groupId')) == group_id]
        members = [p for p in info['pages'] if p.get('groupId') == group_id]
        if len(groups) != 1 or not members:
            raise UpstreamError('Selected group is absent or ambiguous; list current groups again')
        if any(not isinstance(p.get('targetId'), str) or not p['targetId']
               or not isinstance(p.get('url'), str) or not p['url'] for p in members):
            raise UpstreamError('Selected group physical metadata is incomplete; preserve prior bindings')
        targets = {p['targetId'] for p in members}
        aliases = [p for p in info['pages'] if p.get('targetId') in targets]
        if any(not isinstance(p.get('url'), str) or credential_bearing_url(p['url']) for p in aliases):
            raise UpstreamError('Credential-bearing or incomplete recovery URL cannot be returned')
        keys = ('pageId', 'targetId', 'tabId', 'groupId', 'windowId', 'url', 'ownership', 'ownerAgentId')
        recovery_pages = {p['targetId']: p['url'] for p in members}
        if any(p['url'] != recovery_pages[p['targetId']] for p in members):
            raise UpstreamError('Selected physical target has conflicting URLs; inspect again')
        return {'inventoryOnly': True, 'groupId': group_id,
                'pages': [{k: p[k] for k in keys if k in p} for p in aliases],
                'recoveryPages': [{'targetId': target, 'url': url} for target, url in recovery_pages.items()],
                'notice': 'Observation is not ownership or recovery approval. Resume rechecks every current alias, URL and owner.'}

    async def prepare_task(self, *, claim=True, recovery=None):
        if not self.task_state:
            return
        if not self.task_state.task_id:
            raise UpstreamError('Bind the actual task UUID with workspace_cleanup begin before browser work')
        info = await self.inventory()
        self.task_state.checkpoint_session(self.tool_session)
        candidates, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], recovery=recovery)
        if conflicts or (candidates and not claim):
            raise UpstreamError(f'Prior task tabs require ownership reconciliation: {conflicts}')
        for group_id in candidates:
            expected = {p['targetId'] for p in info['pages'] if p.get('groupId') == group_id}
            tabs_schema = next((tool.get('inputSchema', {}) for tool in self.catalog['catalog']['tools']
                                if tool.get('name') == 'tabs'), {})
            if 'reclaim' in tabs_schema.get('properties', {}).get('action', {}).get('enum', []):
                targets = {p['targetId']: p['url'] for p in info['pages'] if p.get('groupId') == group_id}
                await self.internal_tool('tabs', {'action': 'reclaim', 'groupId': group_id,
                    'expected': [{'targetId': target, 'url': url} for target, url in sorted(targets.items())]})
                info = await self.inventory()
                if not expected.issubset({p['targetId'] for p in info['pages'] if p.get('ownership') == 'mine'}):
                    raise UpstreamError('Explicit group recovery remains unverified; preserve prior binding')
                continue
            before = {p['targetId'] for p in info['pages']}
            await self.internal_tool('tabs', {'action':'new', 'groupId':group_id, 'url':'about:blank'})
            info = await self.inventory()
            created = {p['targetId']:p for p in info['pages'] if p['targetId'] not in before and p.get('ownership') == 'mine' and p['url'] == 'about:blank'}
            if len(created) != 1:
                raise UpstreamError('Recovery tab identity ambiguous; inspect without opening more tabs')
            temporary = next(iter(created.values()))
            try:
                # Official tabs new ACK precedes detached group adoption. Wait
                # only for its ownership result; never replay the creation.
                deadline = time.monotonic() + 5
                while not expected.issubset({p['targetId'] for p in info['pages'] if p.get('ownership') == 'mine'}):
                    if time.monotonic() >= deadline:
                        raise UpstreamError('Recorded group was not adopted; preserve prior binding')
                    await asyncio.sleep(0.1)
                    info = await self.inventory()
            finally:
                # This exact empty tab was created only by reconciliation. A
                # failed adoption must not leak it or hide the prior task tabs.
                await self.internal_tool('tabs', {'action':'close', 'page':temporary['pageId']})
                info = await self.inventory()
                if any(p['targetId'] == temporary['targetId'] for p in info['pages']):
                    raise UpstreamError('Recovery tab still present; preserve binding')
            if not expected.issubset({p['targetId'] for p in info['pages'] if p.get('ownership') == 'mine'}):
                raise UpstreamError('Recorded group was not adopted; preserve it and inspect ownership')
        if recovery is not None:
            remaining, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], recovery=recovery)
            if remaining or conflicts:
                raise UpstreamError('Selected group recovery remains unverified; preserve prior bindings')
        self.task_prepared = True
        self.task_state.save(self.tool_session, info['pages'], info['groups'])

    async def remember_task(self):
        if self.task_state and self.task_state.task_id:
            info = await self.inventory()
            candidates, conflicts = self.task_state.reconciliation(info['pages'], info['groups'])
            if candidates or conflicts:
                self.task_prepared = False
                raise UpstreamError('Task ownership changed after the action; preserve prior bindings and reconcile before continuing')
            if self.execution_id:
                self.task_state.record['execution_id'] = self.execution_id
            self.task_state.record['review_required'] = list(self.cleanup.review_required)
            self.task_state.record['used_tabs'] = list(self.cleanup.used.values())
            self.task_state.save(self.tool_session, info['pages'], info['groups'], self.cleanup.retained.items())

    async def start_runtime(self):
        # The launcher verifies publisher/profile and serializes concurrent starts.
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(ROOT / "start-neo.ps1")]
        state = json.loads((ROOT / "runtime-plan.json").read_text(encoding="utf-8"))["server_state_root"]
        # Python preserves PS7 module paths when invoking Windows PowerShell 5.1.
        # Let the child rebuild its own defaults; never change the parent/registry.
        environment = build_environment(os.environ, {"TEMP": state, "TMP": state, "TMPDIR": state},
                                        remove=("PSModulePath",))
        result = await asyncio.to_thread(
            subprocess.run, command, cwd=ROOT, env=environment,
            capture_output=True, timeout=50,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            log = Path(state) / "logs" / f"launcher-failure-{time.time_ns()}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(log, detail, backup_count=0)
            raise UpstreamError(f"Approved neo launcher exited {result.returncode}: "
                                f"{detail[-2000:]} [diagnostic: {log}]")

    def headers(self):
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def tool_request(self, message: dict):
        params = dict(message.get("params") or {})
        arguments = dict(params.get("arguments") or {})
        if "session" not in arguments and self.tool_session:
            arguments["session"] = self.tool_session
        initialization = (self.initialize or {}).get("params", {})
        client_info = initialization.get("clientInfo", {})
        arguments.setdefault("agentName", client_info.get("name", "codex-mcp-client"))
        meta = dict(params.get("_meta") or {})
        meta.update({
            "io.modelcontextprotocol/protocolVersion": TOOL_PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": initialization.get("capabilities", {}),
            "io.modelcontextprotocol/clientInfo": client_info,
        })
        params.update(arguments=arguments, _meta=meta)
        request = dict(message, params=params)
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": TOOL_PROTOCOL,
                   "Mcp-Method": "tools/call", "Mcp-Name": params["name"]}
        return request, headers

    async def post(self, message: dict, *, timeout=None, headers=None):
        if await self.guard_endpoint() is False:
            raise NotReady("Owned runtime is stopped or finishing normal shutdown")
        options: dict[str, Any] = {"headers": self.headers() if headers is None else headers,
                                   "json": message}
        if timeout is not None:
            options["timeout"] = timeout
        async with self.client.stream("POST", self.endpoint, **options) as response:
            if response.status_code == 404:
                raise SessionGone("neo HTTP session expired")
            if response.status_code == 422:
                # Stateful upstream replacing a stateless server rejects the old
                # headerless readiness ping before tool dispatch. Other 422s are
                # ordinary errors, not a reason to replay an action.
                detail = (await response.aread()).decode("utf-8", errors="replace").strip()
                if detail == "Unexpected message, expect initialize request":
                    raise SessionGone("neo requires a new HTTP initialization")
            if response.status_code in (502, 503):
                raise NotReady("neo HTTP runtime is starting")
            if response.status_code >= 400:
                raise UpstreamError(f"neo HTTP status {response.status_code}")
            if message.get("method") == "initialize":
                self.session = response.headers.get("Mcp-Session-Id")
            if "id" not in message or "method" not in message:
                # Client notifications and responses may be acknowledged with 202.
                await response.aread()
                return None
            if "text/event-stream" in response.headers.get("content-type", ""):
                async for item in sse_messages(response):
                    if item.get("id") == message["id"] and "method" not in item:
                        return item
                    self.emit(item)
            else:
                item = json.loads(await response.aread())
                if item.get("id") == message["id"] and "method" not in item:
                    return item
            raise UpstreamError("neo response ended without the matching RPC result")

    async def connect(self, message: dict):
        # A former backend port is not authority for a new transport session.
        self.endpoint = self.canonical_endpoint
        self.session = None
        self.protocol = None
        deadline = time.monotonic() + self.startup_seconds
        started = False
        checked_backend = False
        while True:
            try:
                self.session = None
                reply = await self.post(message, timeout=3)
                if not reply or "result" not in reply:
                    raise UpstreamError("neo initialization was rejected")
                self.protocol = reply["result"]["protocolVersion"]
                return reply
            except (httpx.TransportError, NotReady):
                if not checked_backend and self.backend_resolver is not None:
                    checked_backend = True
                    backend = await asyncio.to_thread(self.backend_resolver)
                    if backend and backend != self.endpoint:
                        # Only initialization is retried. Website tools have not
                        # been dispatched; the logical browser handle is retained.
                        self.endpoint = backend
                        continue
                if not started:
                    await self.starter()
                    started = True
                    checked_backend = False  # The verified child may now exist.
                    self.endpoint = self.canonical_endpoint
                if time.monotonic() >= deadline:
                    raise UpstreamError("neo did not become ready before startup deadline")
                await asyncio.sleep(0.3)

    async def ensure_ready(self):
        async with self.lock:
            if self.initialize is None:
                raise UpstreamError("MCP initialize has not completed")
            self.sequence += 1
            if self.endpoint != self.canonical_endpoint:
                backend = await asyncio.to_thread(self.backend_resolver) if self.backend_resolver else None
                if backend != self.endpoint:
                    self.endpoint = self.canonical_endpoint
                    self.session = self.protocol = None
            try:
                reply = await self.post({"jsonrpc": "2.0", "id": f"neo-ready-{self.sequence}",
                                         "method": "ping"}, timeout=3)
                if not reply or "error" in reply:
                    raise UpstreamError("neo readiness ping failed")
                return
            except (httpx.TransportError, SessionGone, NotReady):
                # No website action has been dispatched. Reinitializing is safe.
                reconnect = dict(self.initialize, id=f"neo-reconnect-{self.sequence}")
                reply = await self.connect(reconnect)
                if self.initialized:
                    await self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.start_events()
                self.emit({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})


    def start_events(self):
        if self.event_task:
            self.event_task.cancel()
        self.event_task = asyncio.create_task(self.events(self.session))

    async def events(self, session):
        # Optional GET channel carries tool-list notifications and server requests.
        while self.session == session:
            try:
                if await self.guard_endpoint() is False:
                    return  # Browser/server is off; wait for actual user work instead of polling.
                async with self.client.stream("GET", self.endpoint, headers=self.headers()) as response:
                    if response.status_code in (404, 405):
                        return
                    if response.status_code != 200:
                        raise UpstreamError(f"neo event stream status {response.status_code}")
                    async for item in sse_messages(response):
                        if self.session == session:
                            self.emit(item)
            except EndpointIdentityError:
                return  # Stop this channel; never poll or send to an unverified owner.
            except (httpx.HTTPError, UpstreamError, ValueError):
                # Readiness is checked before the next request; never restart a tool here.
                pass
            await asyncio.sleep(2)

    async def handle(self, message: dict):
        method = message.get("method")
        sent = False
        try:
            if method == 'tools/call':
                p=message.get('params') or {}
                a=p.get('arguments') or {}
                if not (p.get('name') == CLEANUP_TOOL_NAME and a.get('action') in ('begin','outputs')):
                    await self.guard_endpoint()  # Before task binding/marking or cleanup record changes.
            if method == "ping":
                # Transport heartbeat must not reopen a deliberately closed browser.
                result = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            elif method == "initialize":
                async with self.lock:
                    self.initialize = message
                    value = copy.deepcopy(self.catalog['initialize'])
                    value['protocolVersion'] = message['params']['protocolVersion']
                    value['instructions'] = 'Workspace rules take precedence over the generic upstream tab advice below: call workspace_cleanup action=begin with the actual task UUID before browser work; resume prior recorded task tabs before opening any new tab. Reuse existing task tabs, and register actually reused business tabs with workspace_cleanup use/pageIds/reason; check mine plus registered physical targets at completion. Use/defer never grant close rights. ' + value.get('instructions','')
                    if self.cleanup.task_id:
                        value['instructions'] = f'Workspace task_id={self.cleanup.task_id} is already bound. Reuse prior recorded pages. Before final response call workspace_cleanup check including registered used targets; close disposable mine tabs, retain only specific next use or unsaved/pending work, and verify absence. For retain/release pass pageIds as an integer array, not page_id or pages; retain also requires a concrete reason. Every cleanup call requires task_id and has no session argument. ' + value['instructions']
                    result = {'jsonrpc':'2.0', 'id':message['id'], 'result':value}
            elif method == "notifications/initialized":
                self.initialized = True
                return
            elif method == 'tools/list':
                value = copy.deepcopy(self.catalog['catalog'])
                # Reclaim is an internal ledger-validated recovery operation, not
                # an agent-selected way to acquire arbitrary existing groups.
                for tool in value['tools']:
                    if tool.get('name') == 'tabs':
                        properties = tool.get('inputSchema', {}).get('properties', {})
                        action = properties.get('action', {})
                        if isinstance(action.get('enum'), list):
                            action['enum'] = [item for item in action['enum'] if item != 'reclaim']
                        properties.pop('expected', None)
                value['tools'].append(CLEANUP_TOOL)
                result = {'jsonrpc':'2.0','id':message['id'],'result':value}
            elif method in ('resources/list', 'resources/templates/list', 'prompts/list'):
                key = {'resources/list':'resources','resources/templates/list':'resourceTemplates','prompts/list':'prompts'}[method]
                result = {'jsonrpc':'2.0','id':message['id'],'result':{key:[]}}
            elif method == "notifications/cancelled":
                request_id = (message.get("params") or {}).get("requestId")
                active = self.active_tools.get(request_id)
                if active:
                    # Closing this request's HTTP stream cancels its server context.
                    active.cancel()
                else:
                    await self.post(message)
                return
            elif method is None:
                await self.post(message)
                return
            elif method == "tools/call" and (message.get("params") or {}).get("name") == CLEANUP_TOOL_NAME:
                async with self.tool_lock:
                    result = await self.cleanup_call(message)
            elif method == "tools/call":
                self.active_tools[message["id"]] = asyncio.current_task()
                try:
                    # Serialize first-call minting and handle replacement; concurrent
                    # requests must not fork this connection's browser identity.
                    async with self.tool_lock:
                        params = message.get("params") or {}
                        args = params.get("arguments") or {}
                        if params.get('name') == 'tabs' and (args.get('action') == 'reclaim' or 'expected' in args):
                            raise ValueError('Task recovery is internal; use workspace_cleanup begin to reconcile recorded tabs')
                        requested_session = args.get("session")
                        if "session" in args:
                            if not isinstance(requested_session, str) or not requested_session:
                                raise ValueError('session must be a nonempty current browser handle')
                            if requested_session != self.tool_session:
                                self.tool_session = requested_session
                                self.task_prepared = False
                        inventory_only = (params.get("name") in ("tabs", "windows", "tab_groups")
                                          and args.get("action") == "list"
                                          and set(args) <= {"action", "session", "agentName"})
                        clean_session = self.tool_session
                        clean_close = (self.cleanup_empty_session == self.tool_session
                                       and self.tool_session is not None
                                       and args.get("session", self.tool_session) == self.tool_session
                                       and params.get("name") == "windows"
                                       and args.get("action") == "close"
                                       and type(args.get("windowId")) is int)
                        # Any new browser work invalidates prior completion. Only
                        # an exact native close acknowledgement can restore it.
                        self.cleanup_empty_session = self.cleanup_closed_session = None
                        if self.task_state and self.task_state.record:
                            self.task_state.record['browser_closed'] = False
                        if self.task_state and not self.task_state.task_id:
                            raise UpstreamError('Bind the actual task UUID with workspace_cleanup begin before browser work')
                        if self.task_state:
                            self.task_state.mark_execution(self.execution_id)
                        await self.ensure_ready()
                        # Inventory observes conflicts without claiming groups or
                        # replacing the saved task's previous physical bindings.
                        if not inventory_only:
                            await self.prepare_task()
                        request, headers = self.tool_request(message)
                        sent = True
                        existing_outputs, baseline_error = None, None
                        if self.task_state:
                            try:
                                existing_outputs = self.task_state.output_baseline(params.get('name'))
                            except Exception as error:
                                baseline_error = error
                        dispatched_ns = time.time_ns()
                        result = await self.post(request, headers=headers)
                        handle = ((result or {}).get("result", {}).get("_meta") or {}).get(SESSION_META_KEY)
                        if isinstance(handle, str) and handle:
                            self.tool_session = handle
                        upstream = (result or {}).get("result") or {}
                        if self.task_state:
                            try:
                                if baseline_error is not None:
                                    raise baseline_error
                                self.task_state.record_output(params.get('name'), upstream, dispatched_ns,
                                                              message['id'], self.tool_session, existing_outputs)
                            except Exception as error:
                                upstream.setdefault('content', []).append({'type':'text', 'text':
                                    f'WORKSPACE_OUTPUT_TRACKING_FAILED: {type(error).__name__}: {error}. Original upstream response is authoritative; do not replay. Output ownership remains unverified; preserve the file.'})
                        if (clean_close and self.tool_session == clean_session and not upstream.get("isError")
                                and any(c.get("type") == "text" and c.get("text") == f"closed window {args['windowId']}"
                                        for c in upstream.get("content", []))):
                            self.cleanup_closed_session = self.tool_session
                            if self.task_state and self.task_state.record:
                                self.task_state.record['browser_closed'] = True
                                self.task_state.save(self.tool_session, [], [])
                        if not inventory_only and not clean_close and result and 'result' in result:
                            try:
                                await self.remember_task()
                            except Exception as error:
                                # The original tool did return. A bookkeeping
                                # failure must never make its action replayable.
                                upstream.setdefault('content',[]).append({'type':'text','text':
                                    f'WORKSPACE_TRACKING_FAILED: {type(error).__name__}: {error}. Original action response above is authoritative; do not replay. Reconcile task tabs before ending.'})
                finally:
                    self.active_tools.pop(message["id"], None)
            else:
                raise UpstreamError('Unsupported non-browser MCP method; browser was not started')
            if result is not None:
                self.emit(result)
        except Exception as error:
            if "id" not in message or "method" not in message:
                print(f"neo MCP {method or 'response'} failed: {type(error).__name__}", file=sys.stderr)
                return
            detail = f"{type(error).__name__}: {error}"
            if method == "tools/call" and (message.get("params") or {}).get("name") == CLEANUP_TOOL_NAME:
                args = (message.get("params") or {}).get("arguments") or {}
                active = args.get("stop_hook_active") is True
                if args.get("action") == "check" and isinstance(error, httpx.ConnectError):
                    # An unreachable endpoint proves no cleanup outcome. Preserve
                    # task bindings and retention, but do not turn browser OFF into
                    # repeated Stop continuations or start it just for this check.
                    value = {"cleanupStatus": "unverified", "cleanupVerified": False,
                             "diagnostic": detail}
                else:
                    value = {"systemMessage": detail} if active else {"decision": "block", "reason": detail}
                # Native hooks parse text with a strict Stop output schema;
                # diagnostic fields stay in MCP structuredContent, not hook input.
                hook_value = {"suppressOutput": True} if value.get("cleanupVerified") is False else value
                self.emit({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps(hook_value)}], "structuredContent": value}})
            elif method == "tools/call":
                sent = sent and not isinstance(error, EndpointIdentityError)
                status = "OUTCOME_UNKNOWN" if sent else "NEO_NOT_READY"
                guidance = ("Request was dispatched and will NOT be replayed. Inspect the actual page/saved result before retrying any action."
                            if sent else "Tool request was NOT dispatched. Fix runtime readiness before invoking the tool again.")
                self.emit({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": f"{status}: {guidance} {detail}"}]}})
            else:
                self.emit({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32000, "message": detail}})

    async def cleanup_call(self, message):
        args = (message.get("params") or {}).get("arguments") or {}
        schema = CLEANUP_TOOL['inputSchema']
        if (not isinstance(args, dict) or set(args) - schema['properties'].keys()
                or any(key not in args for key in schema['required'])):
            raise ValueError('workspace_cleanup requires action and task_id. Allowed fields: action, task_id, pageIds, reason, condition (defer only), stop_hook_active, recovery (resume only), group_id (inventory only). For retain/release use pageIds=[integer pageId]; retain requires reason. No session argument. No task state or browser was changed.')
        if 'condition' in args and args.get('action') != 'defer':
            raise ValueError('condition is accepted only by defer')
        if 'recovery' in args and args.get('action') != 'resume':
            raise ValueError('Explicit recovery is accepted only by resume')
        if 'group_id' in args and args.get('action') != 'inventory':
            raise ValueError('group_id is accepted only by inventory')
        if args.get('action') == 'inventory' and (
                set(args) != {'action', 'task_id', 'group_id'}
                or not isinstance(args['group_id'], str) or not args['group_id'].strip()):
            raise ValueError('inventory accepts only action, task_id and a nonempty exact group_id')
        self.bind_task(args.get("task_id"))
        if args.get('action') == 'inventory':
            # Resolve the recovery-input/prepare_task cycle through a fixed read,
            # never by allowing arbitrary run code past reconciliation.
            value = await self.recovery_inventory(args['group_id'])
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                      'structuredContent': value}
            if self.tool_session:
                result['_meta'] = {SESSION_META_KEY: self.tool_session}
            return {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
        if args.get('action') == 'outputs':
            if set(args) != {'action', 'task_id'}:
                raise ValueError('outputs accepts only action and task_id; it never changes or deletes files')
            if not self.task_state:
                raise ValueError('Durable task state is unavailable; output ownership cannot be inspected')
            value = self.task_state.outputs()
            return {'jsonrpc':'2.0', 'id':message['id'], 'result':{
                'content':[{'type':'text','text':json.dumps(value, ensure_ascii=False)}], 'structuredContent':value}}
        if args.get('action') == 'begin':
            record = self.task_state.record if self.task_state else {}
            value = {'taskBound':True, 'browserStarted':False, 'priorPages':record.get('pages',[]), 'resumeRequired':bool(record.get('pages')), 'priorUsedTabs':record.get('used_tabs',[])}
            return {'jsonrpc':'2.0','id':message['id'],'result':{'content':[{'type':'text','text':json.dumps(value)}], 'structuredContent':value}}
        if args.get('action') == 'resume':
            if self.task_state:
                self.task_state.mark_execution(self.execution_id)
            await self.ensure_ready()
            self.task_prepared = False
            await self.prepare_task(recovery=args.get('recovery'))
            args = dict(args, action='check')
        if args.get("action") not in ("check", "retain", "release", "use", "defer"):
            raise ValueError("Unsupported cleanup action")
        if args.get('action') in ('use','defer'):
            if not self.task_state or not self.tool_session:
                raise ValueError('use/defer require a durable bound task and already-running browser session')
            self.task_state.mark_execution(self.execution_id)
        # A Stop hook must never reopen a browser, mint a session, or scan the
        # global profile when this connection has not done browser work.
        if args.get("action") == "check" and self.cleanup_closed_session == self.tool_session and self.tool_session is not None:
            value = {"pendingPageIds": [], "retained": [], "browserClosed": True}
        elif self.tool_session is None:
            if self.cleanup.used:
                raise UpstreamError('Recorded used task targets lack a current browser handle; cleanup is unverified')
            value = {"pendingPageIds": [], "retained": [], "browserUsed": False}
        else:
            value = await self.review_task_tabs(args)
        return {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}}

    async def review_task_tabs(self, args):
        """Inspect an existing session only; no readiness startup or action replay."""
        if self.task_state:
            # Recheck current ownership even after successful browser work.
            # A check may inspect a running browser, but never start one.
            await self.prepare_task(claim=False)
        self.cleanup_empty_session = self.cleanup_closed_session = None
        self.sequence += 1
        call = {"jsonrpc": "2.0", "id": f"neo-cleanup-{self.sequence}", "method": "tools/call", "params": {
            "name": "run", "arguments": {"session": self.tool_session, "code": "const pages=await browser.pages.list(); const targets=(await browser.cdp(\"Target.getTargets\",{})).targetInfos.map(t=>t.targetId); return pages.filter(p=>!p.targetId || targets.includes(p.targetId));"}}}
        request, headers = self.tool_request(call)
        reply = await self.post(request, headers=headers, timeout=20)
        result = (reply or {}).get("result") or {}
        returned = (result.get("_meta") or {}).get(SESSION_META_KEY)
        if returned and returned != self.tool_session:
            # Server restart/expiry does not establish ownership inheritance.
            self.tool_session = returned
            raise UpstreamError("Neo replaced the browser session. Reconcile prior task tabs before ending work")
        structured = result.get("structuredContent") or {}
        if result.get("isError") or structured.get("ok") is not True:
            raise UpstreamError("Neo cleanup listing failed; inspect the current connection and task ownership")
        physical = None
        scope_session = self.tool_session
        if self.cleanup.used or args.get('action') in ('use','defer'):
            physical = await self.internal_tool('run', {'code': 'return {tabs:(await browser.cdp("Browser.getTabs",{})).tabs, targets:(await browser.cdp("Target.getTargets",{})).targetInfos.map(t=>t.targetId), pages:await browser.pages.list()};'})
            physical = (physical.get('structuredContent') or {})
            if physical.get('ok') is not True or not isinstance(physical.get('value'),dict):
                raise UpstreamError('Used physical inventory unavailable; preserve task scope')
            physical = physical['value']
            if scope_session != self.tool_session:
                raise UpstreamError('Browser session changed during used scope inspection')
        value = self.cleanup.review(self.tool_session, physical['pages'] if physical else structured.get("value"), args,
            physical_tabs=physical.get('tabs') if physical else None, targets=physical.get('targets') if physical else None)
        if self.task_state:
            self.task_state.save_used(self.cleanup.used.values())
        if self.task_state and self.execution_id and args.get('action') == 'check':
            self.task_state.record['finish_checked_execution'] = (
                self.execution_id if not has_pending_cleanup(value) else None)
        if self.task_state and args.get('action') == 'check' and not has_pending_cleanup(value):
            self.task_state.record['execution_incomplete'] = False
        await self.remember_task()
        if args.get("action") == "check" and not value["pendingPageIds"] and not value["retained"] and not self.cleanup.used:
            self.cleanup_empty_session = self.tool_session
        return value

    async def close(self):
        if self.event_task:
            self.event_task.cancel()
            await asyncio.gather(self.event_task, return_exceptions=True)
        await self.client.aclose()


async def main():
    plan = json.loads((ROOT / "runtime-plan.json").read_text(encoding="utf-8"))
    def emit(message):
        print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', default=os.environ.get('WORKSPACE_NEO_TASK_ID') or os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--client', choices=('codex','agy'), default='codex')
    args = parser.parse_args()
    catalog = json.loads((ROOT / 'official-tools.json').read_text(encoding='utf-8'))
    if catalog.get('server_sha256') != plan['server_override']['sha256']:
        raise UpstreamError('Official schema catalog does not match the approved server; refresh it without launching a browser')
    from neo_transport import verified_backend_endpoint
    from neo_endpoint_identity import assert_owned_endpoint
    bridge = Bridge(plan["mcp_endpoint"], emit, startup_seconds=55, catalog=catalog,
                    backend_resolver=None,
                    endpoint_guard=lambda endpoint: assert_owned_endpoint(plan, endpoint),
                    state_root=Path(plan['ledger_root']) / 'task-tabs' / args.client, task_id=args.task_id)
    pending: set[asyncio.Task] = set()
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            try:
                message = json.loads(line)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ValueError("Expected a JSON-RPC object")
            except (ValueError, UnicodeDecodeError):
                emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC input"}})
                continue
            if message.get("method") in ("initialize", "notifications/initialized"):
                await bridge.handle(message)
                continue
            task = asyncio.create_task(bridge.handle(message))
            pending.add(task)
            task.add_done_callback(pending.discard)
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await bridge.close()


if __name__ == "__main__":
    # MCP stdio is UTF-8 regardless of Windows' locale or a client's Python flags.
    # AGY starts this executable without Codex's -X utf8 option.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=GuardedProactorEventLoop) as runner:
            runner.run(main())
    else:
        asyncio.run(main())
