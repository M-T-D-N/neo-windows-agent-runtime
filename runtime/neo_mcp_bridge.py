"""Workspace-owned stdio transport for the approved BrowserOS neo HTTP MCP.

Discovers tools locally; starts the dedicated runtime only for browser work. Only initialization/readiness is
retried: a dispatched tool call is never replayed, including after response loss.
The bridge adds local workflow guidance and opt-in content verification.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

TOOL_PROTOCOL = "2026-07-28"
SESSION_META_KEY = "com.browseros.neo/session"
INVENTORY_META_KEY = "com.workspace.neo/inventory_receipt"
WINDOW_BLOCK_META_KEY = "com.workspace.neo/window_restore_blocked"
INVENTORY_PROPERTY = {"type":"string", "minLength":1, "description":
    "After a verified browser reset, echo the inventory_receipt returned by this connection's explicit tabs action=list. Select current page IDs/session from that result and issue a new request; never replay an old page request."}
RECONNECT_GUIDANCE = (
    'Reconnect initialization is shared by Codex and AGY: binding the actual task UUID initializes '
    'locally without opening Neo; if it is not supplied by the host, call workspace_cleanup begin. '
    'After a recorded reset, tabs action=list works without a second begin. A premature page/cleanup '
    'action returns NEO_RECONNECT_INITIALIZED with only a fresh tab catalog: requestedActionExecuted=false. '
    'It never executes or replays that original action. Select current IDs/session and construct a new '
    'request using that connection\'s inventory_receipt. Check/outputs never launch Neo. ')
HOST_RECONNECT_GUIDANCE = (
    'If even local MCP calls fail with Transport closed, use this host’s supported MCP '
    'reconnect (mcp_control.restart where available), then verify from this same chat. '
    'A running browser does not prove this client is connected. Never replay an '
    'unconfirmed dispatched action. ')
DISABLED_HELP_TOOLS = ('request_human_help', 'await_human_help')
CHAT_HELP_GUIDANCE = (
    'If a human must complete a sign-in, verification, account choice, or approval, ask in chat. '
    'Pause the affected browser work. On return, inspect the same tab and verify the needed step '
    'before resuming within the original authorization. ')
READ_FRAME = re.compile(r'^\[UNTRUSTED_PAGE_CONTENT nonce=([a-f0-9]{16}) origin=[^\n]*\][^\n]*\n')
READ_CONTENT_PROPERTY = {'type': 'boolean', 'description':
    'Require nonempty rendered content for a markdown/text read. Use with a selector for the expected '
    'page region. If Neo cannot verify content, inspect the current tab and page state, including '
    'dialogs or an explicit empty-state message. This check does not navigate or reload.'}
NAVIGATION_RESTORE_BLOCKS = {
    'Ambiguous normal windows; no automatic restoration': 'multiple_windows',
    'Cannot identify one existing native browser window': 'window_identity_unavailable',
    'Native browser window was replaced or became ambiguous; no action dispatched': 'window_identity_changed',
    'Foreground unavailable or changed; leave window untouched': 'foreground_unavailable',
    'Foreground changed during background placement; no show dispatched': 'foreground_changed',
    'Non-activating restore failed': 'restore_failed',
    'Non-activating restoration not confirmed; action not dispatched': 'restore_unverified',
}
NAVIGATION_RESTORE_GUIDANCE = (
    'NEO_WINDOW_RESTORE_BLOCKED: Neo reached the browser, and navigation was stopped before dispatch. '
    'Keep the existing task tab. Use tabs list/read to check the same physical page. '
    'If the task window is still inaccessible, restore that existing window from the tray or resume '
    'after Windows is unlocked. When the window is available, select the current page and issue a fresh '
    'navigation request. Preserve the original tab and do not restart MCP or create a replacement for '
    'this status alone.')

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from process_environment import build_environment
from workspace_storage import atomic_write_text
from neo_cleanup import CleanupPolicy, TOOL as CLEANUP_TOOL, TOOL_NAME as CLEANUP_TOOL_NAME, has_pending_cleanup
from neo_task_state import TaskState
from neo_tab_identity import credential_bearing_url, other_owner
from neo_transport import BrowserRecordStale, OwnedEndpointTransition


def apply_result_guidance(name, upstream, require_content):
    content = upstream.get('content') or []
    if upstream.get('isError'):
        # Only the dedicated navigation tool can certify one action was skipped.
        # A failed run may have executed earlier effects before reaching this guard.
        first = content[0] if content else {}
        prefix = 'navigate failed: '
        if name == 'navigate' and first.get('type') == 'text' and first.get('text', '').startswith(prefix):
            reason = NAVIGATION_RESTORE_BLOCKS.get(first['text'][len(prefix):])
            if reason:
                upstream.setdefault('_meta', {})[WINDOW_BLOCK_META_KEY] = {
                    'reason': reason, 'requestedActionExecuted': False}
                content.append({'type': 'text', 'text': NAVIGATION_RESTORE_GUIDANCE})
        return
    if name != 'read' or not require_content:
        return
    for block in content:
        if block.get('type') != 'text':
            continue
        text = block.get('text', '')
        start = READ_FRAME.match(text)
        if start:
            end = text.find(f'\n[END_UNTRUSTED_PAGE_CONTENT nonce={start[1]}]', start.end())
            if end >= 0 and text[start.end():end].strip() not in ('', '(empty)'):
                return
    upstream['isError'] = True
    upstream.setdefault('content', []).append({'type': 'text', 'text':
        'NEO_CONTENT_UNVERIFIED: The read did not verify nonempty rendered content. Keep the '
        'same physical tab and inspect the result above, the expected page region, and any '
        'pending dialog. A successful navigation or PDF does not prove the destination '
        'loaded. Do not count a blank page as an empty list or repeat print/save.'})


def record_bridge_exit(log_root, client, task_id, started_at, reason, active_tool_handlers):
    """Keep bounded, sanitized last-exit evidence, even on a forced stdio exit."""
    try:
        task = str(uuid.UUID(task_id)) if task_id else None
    except ValueError:
        task = None
    slot = f'{sha256(task.encode("ascii")).digest()[0] % 64:02d}' if task else 'unbound'
    # The task UUID only selects a bounded slot. The handler count excludes
    # completed handlers whose responses may have been lost at the host pipe.
    record = {'time_utc': datetime.now(timezone.utc).isoformat(),
              'started_at_utc': started_at, 'pid': os.getpid(), 'parent_pid': os.getppid(),
              'client': client, 'reason': reason, 'active_tool_handlers': active_tool_handlers}
    try:
        log_root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(log_root / f'bridge-exit-{client}-{slot}.json',
                          json.dumps(record, separators=(',', ':')) + '\n', backup_count=0)
    except Exception:
        # Diagnostics must not prevent teardown or write to the MCP output pipe.
        try:
            print(f'NEO_BRIDGE_EXIT: {reason} (exit record unavailable)', file=sys.stderr, flush=True)
        except OSError:
            pass


class UpstreamError(Exception):
    pass


class EndpointIdentityError(UpstreamError):
    pass


class BrowserGenerationChanged(EndpointIdentityError):
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
        on_wakeup_eof: Callable[[], None] | None = None

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
                        try:
                            if self.on_wakeup_eof:
                                self.on_wakeup_eof()
                            print("NEO_LOOP_CHANNEL_CLOSED: Windows internal socket reached EOF; "
                                  "bridge exiting to prevent CPU spin. Dispatched actions "
                                  "must be inspected before retry; nothing is replayed.",
                                  file=sys.stderr, flush=True)
                        finally:
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
                 backend_resolver=None, endpoint_guard=None, browser_generation=None):
        address = urlsplit(endpoint)
        if (address.scheme != "http" or address.hostname != "127.0.0.1"
                or address.path != "/mcp" or address.username or address.password
                or address.query or address.fragment):
            raise ValueError("Only the approved loopback /mcp endpoint is supported")
        self.endpoint = endpoint
        self.canonical_endpoint = endpoint
        self.backend_resolver = backend_resolver
        self.endpoint_guard = endpoint_guard
        self.browser_generation = browser_generation
        self.endpoint_browser = None
        self.endpoint_epoch = 0
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
        self.reset_pending = False
        self.reset_requires_begin = False
        self.reset_requires_inventory = False
        self.inventory_receipt = None
        self.catalog = catalog or json.loads((ROOT / 'official-tools.json').read_text(encoding='utf-8'))
        self.task_state = TaskState(state_root) if state_root else None
        self.task_prepared = False
        self.execution_id = os.environ.get('WORKSPACE_NEO_EXECUTION_ID')
        if task_id and self.task_state:
            self.bind_task(task_id)

    async def endpoint_observation(self):
        if self.endpoint_guard is not None:
            return await asyncio.to_thread(self.endpoint_guard, self.endpoint)
        if self.browser_generation is not None:
            return await asyncio.to_thread(self.browser_generation)

    async def guard_endpoint(self, *, defer_recovery=False):
        try:
            identity = await self.endpoint_observation()
        except (BrowserRecordStale, OwnedEndpointTransition):
            if defer_recovery:
                return False
            raise
        except Exception as error:
            raise EndpointIdentityError('Endpoint ownership check failed before dispatch: '+str(error)) from error
        if isinstance(identity, tuple):
            if self.endpoint_browser is not None and self.endpoint_browser != identity:
                if defer_recovery:
                    return False
                raise BrowserGenerationChanged('Verified browser process changed')
            self.endpoint_browser = identity
        return identity

    async def reconcile_endpoint_browser(self, error):
        if isinstance(error, BrowserRecordStale):
            # This entry point only rebinds an existing verified CDP owner.
            # It never launches a browser, stops a server or sends an RPC.
            await self.run_runtime_script('ensure-neo-mcp.ps1', '-RepairOwnedBrowser')
        identity = await self.endpoint_observation()
        if not isinstance(identity, tuple):
            raise UpstreamError('Browser identity was not ready after recovery; no browser was launched')
        self.endpoint_browser = identity
        self.endpoint_epoch += 1
        self.session = self.protocol = None
        self.task_prepared = False
        self.cleanup_empty_session = self.cleanup_closed_session = None
        self.reset_requires_inventory = True
        self.inventory_receipt = None
        raise UpstreamError('Browser replacement verified. Call tabs action=list and select current IDs; '
                            'the original request was not dispatched.')

    def bind_task(self, task_id, *, refresh_scope=False):
        self.cleanup.bind(task_id)
        if self.task_state:
            was_bound = self.task_state.task_id is not None
            prior_reset = (self.task_state.record or {}).get('browser_reset_id')
            prior_pending = (self.task_state.record or {}).get('browser_reset_pending')
            prior_retirement = (self.task_state.record or {}).get('retirement_id')
            previous = self.task_state.record
            record = self.task_state.bind(task_id, refresh_scope=refresh_scope)
            retired = record.get('retirement_id') != prior_retirement
            reset_changed = was_bound and (record.get('browser_reset_id') != prior_reset or
                prior_pending is True and record.get('browser_reset_pending') is False)
            if retired or record.get('browser_reset_id') and (not was_bound or record.get('browser_reset_id') != prior_reset):
                # Binding the actual task is the local initialization step.
                # A second model-authored `begin` cannot add identity evidence.
                # Fresh caller-visible inventory still gates all page actions.
                self.reset_requires_begin = False
                self.reset_requires_inventory = True
                self.inventory_receipt = None
            if retired:
                self.tool_session = record.get('session')
                self.task_prepared = False
                self.cleanup_empty_session = self.cleanup_closed_session = None
            if reset_changed:
                self.tool_session = None
                self.task_prepared = False
                self.cleanup_empty_session = self.cleanup_closed_session = None
                self.reset_pending = record.get('browser_reset_pending') is True
            if record.get('browser_reset_pending'):
                self.reset_pending = True
            if self.tool_session is None:
                self.tool_session = record['session']
            if previous is not record:
                self.cleanup_empty_session = self.cleanup_closed_session = None
            if record.get('browser_closed') and not record.get('pages') and not record.get('used_tabs'):
                self.cleanup_closed_session = self.tool_session
            if not was_bound or reset_changed or retired or previous is not record:
                self.cleanup.retained = {tuple(k):v for k,v in record.get('retained',[])}
                new_boundary = not was_bound or reset_changed or retired
                self.cleanup.review_required = (
                    set(self.cleanup.retained) if new_boundary else
                    {tuple(key) for key in record.get('review_required', [])})
                self.cleanup.used = {item['targetId']:item for item in record.get('used_tabs',[])}
                for item in self.cleanup.used.values():
                    if new_boundary and item.get('deferred'):
                        item['deferred']['review_required'] = True

    def refresh_reset_binding(self, message):
        """Observe a verified close-all boundary without starting the browser."""
        if self.task_state and self.task_state.task_id:
            self.bind_task(self.task_state.task_id, refresh_scope=True)
            record = self.task_state.record
            if record.get('browser_reset_id') or record.get('retirement_id'):
                params = dict(message.get('params') or {})
                arguments = dict(params.get('arguments') or {})
                # All previous browser handles were retired by close-all. Do
                # not let an old conversation hint reintroduce one on first use.
                if arguments.get('session') != self.tool_session:
                    arguments.pop('session', None)
                    params['arguments'] = arguments
                    return dict(message, params=params)
        return message

    @asynccontextmanager
    async def browser_operation(self, *, local_only=False, exclusive=False):
        if self.task_state is None or local_only:
            yield
        else:
            async with self.task_state.browser_operation(exclusive=exclusive) as guard:
                if self.task_state.task_id:
                    async with self.task_state.task_operation():
                        yield guard
                else:
                    yield guard

    def consume_reset_receipt(self):
        if self.reset_pending:
            try:
                self.task_state.consume_browser_reset_handle(self.tool_session)
            except ValueError as error:
                self.bind_task(self.task_state.task_id)
                raise UpstreamError('Reset was completed by another connection; inspect the current task state before continuing') from error
            self.reset_pending = False

    async def reconnect_inventory(self, message):
        """Return only a fresh catalog; never execute the submitted old action."""
        if not self.task_state or not self.task_state.task_id:
            raise UpstreamError('Bind the actual task UUID before reconnecting; no task was inferred')
        # A stale close/use/resume request is not intent to reopen a browser.
        # Reconnect HTTP safely if needed, but never invoke the runtime starter.
        await self.ensure_ready(allow_start=False)
        upstream = await self.internal_tool('tabs', {'action': 'list'})
        if not any(c.get('type') == 'text' and c.get('text', '').strip()
                   for c in upstream.get('content', [])):
            raise UpstreamError('Reconnect inventory was empty; no page action was dispatched')
        receipt = str(uuid.uuid4())
        self.inventory_receipt = receipt
        upstream.setdefault('_meta', {})[INVENTORY_META_KEY] = receipt
        upstream['isError'] = True  # The requested operation did not execute.
        upstream['structuredContent'] = {
            'initializationOnly': True, 'taskBound': True,
            'task_id': self.task_state.task_id, 'freshInventoryProvided': True,
            'requestedActionExecuted': False, 'requiresFreshRequest': True,
            'inventory_receipt': receipt, 'session': self.tool_session,
        }
        upstream.setdefault('content', []).insert(0, {'type': 'text', 'text':
            'NEO_RECONNECT_INITIALIZED: Task binding and fresh tab inventory are complete. '
            'The original request was NOT dispatched and will NOT be replayed. '
            'Inspect the catalog below, select current page IDs/session, then construct a NEW '
            f'request with inventory_receipt={receipt}. Do not repeat an unverified save/upload/post.'})
        return {'jsonrpc': '2.0', 'id': message['id'], 'result': upstream}

    async def internal_tool(self, name, arguments, *, observation_session=None):
        self.sequence += 1
        call = {'jsonrpc':'2.0', 'id':f'neo-task-{self.sequence}', 'method':'tools/call',
                'params':{'name':name, 'arguments':arguments}}
        request, headers = self.tool_request(call)
        reply = await self.post(request, headers=headers, timeout=20)
        if not reply or 'error' in reply or 'result' not in reply:
            raise UpstreamError('Task reconciliation RPC failed; preserve existing bindings')
        result = (reply or {}).get('result') or {}
        handle = (result.get('_meta') or {}).get(SESSION_META_KEY)
        if observation_session is not None:
            if self.reset_pending or handle != observation_session:
                raise UpstreamError('Neo session changed during assessment; call tabs action=list separately')
        elif handle:
            self.tool_session = handle
            self.consume_reset_receipt()
        if result.get('isError'):
            if name == 'tabs' and arguments.get('action') == 'reclaim_restored' and any(
                    item.get('type') == 'text' and item.get('text') in (
                        'reclaim_expired: No complete normally closed original task proof',
                        'reclaim_expired: No complete original task closure and claim proof',
                    ) for item in result.get('content', []) if isinstance(item, dict)):
                raise UpstreamError(
                    'NEO_RESTORE_CLOSURE_PROOF_MISSING: The server could not verify the prior '
                    'session closure and exact released tab claims. Preserve the original binding '
                    'and restored tabs; do not retry unchanged recovery inputs.')
            raise UpstreamError('Task reconciliation failed; no browser action may be replayed')
        return result

    async def inventory(self):
        result = await self.internal_tool('run', {'code':
            'const pages=await browser.pages.list(); const targets=(await browser.cdp("Target.getTargets",{})).targetInfos.map(t=>t.targetId); '
            'const groups=(await browser.cdp("Browser.getTabGroups",{})).groups; '
            'return {pages,groups,targets};'})
        structured = result.get('structuredContent') or {}
        if structured.get('ok') is not True or not isinstance(structured.get('value'), dict):
            raise UpstreamError('Task inventory unavailable; preserve prior bindings')
        info = structured['value']
        pages, targets = info.get('pages'), info.get('targets')
        if (not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages)
                or not isinstance(targets, list)
                or any(not isinstance(t, str) or not t for t in targets)
                or any(p.get('targetId') is not None and
                       (not isinstance(p['targetId'], str) or not p['targetId']) for p in pages)):
            raise UpstreamError('Incomplete native target inventory; preserve prior bindings')
        missing = sorted({p['targetId'] for p in pages if p.get('targetId')} - set(targets))
        if missing:
            # SDK aliases can outlive closed tabs. Only the explicit CDP absence
            # error proves an omitted alias closed; live/unknown probes preserve
            # the saved task, rather than filtering away a catalog disagreement.
            proof = await self.internal_tool('run', {'code':
                'const absent=[]; for (const targetId of ' + json.dumps(missing) + ') {'
                'try {await browser.cdp("Target.getTargetInfo",{targetId}); return {absent:[],present:targetId};}'
                'catch(e) {if(e.message!=="CDP error: No target with given id found") throw e; absent.push(targetId);}} '
                'return {absent};'})
            evidence = proof.get('structuredContent') or {}
            value = evidence.get('value')
            if (evidence.get('ok') is not True or not isinstance(value, dict)
                    or value.get('absent') != missing or value.get('present') is not None):
                raise UpstreamError('Native/SDK target inventory disagrees; preserve prior bindings')
        return dict(info, pages=[p for p in pages if not p.get('targetId') or p['targetId'] in targets])

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

    async def verify_retirement_absence(self, record, selection, *, abandon=False, observation_session=None):
        """Use full native inventory, including targets hidden from the SDK."""
        observed = await self.internal_tool('run', {'code':
            'return {tabs:(await browser.cdp("Browser.getTabs",{includeHidden:true})).tabs, '
            'windows:(await browser.cdp("Browser.getWindows",{})).windows, '
            'groups:(await browser.cdp("Browser.getTabGroups",{})).groups, '
            'targets:(await browser.cdp("Target.getTargets",{})).targetInfos, '
            'pages:await browser.pages.list()};'}, observation_session=observation_session)
        result = observed.get('structuredContent') or {}
        info = result.get('value')
        if result.get('ok') is not True or not isinstance(info, dict):
            raise UpstreamError('Complete browser inventory unavailable; preserve the recorded binding')
        rows = [info.get(key) for key in ('tabs', 'groups', 'targets', 'pages', 'windows')]
        if any(not isinstance(group, list) or any(not isinstance(item, dict) for item in group)
               for group in rows):
            raise UpstreamError('Incomplete browser inventory; preserve the recorded binding')
        tabs, groups, targets, pages, windows = rows
        group_ids = [g.get('id', g.get('groupId')) for g in groups]
        ids = [t.get('targetId') for t in targets]
        window_ids = [w.get('windowId') for w in windows]
        if (any(not isinstance(i, str) or not i for i in ids + group_ids)
                or len(set(ids)) != len(ids) or len(set(group_ids)) != len(group_ids)
                or any(type(i) is not int for i in window_ids)
                or len(set(window_ids)) != len(window_ids)
                or any(not isinstance(t.get('type'), str) for t in targets)
                or any(not isinstance(t.get('url'), str) for t in targets + tabs + pages)
                or any(not isinstance(p.get('targetId'), str) or not p['targetId']
                       for p in tabs + pages)):
            raise UpstreamError('Invalid browser inventory identity; preserve the recorded binding')
        physical = self.cleanup._physical_inventory(tabs, ids)
        native_targets = {row['targetId']: row for row in targets}
        windows_by_id = dict(zip(window_ids, windows))
        groups_by_id = dict(zip(group_ids, groups))
        non_tab_types = {'background_page', 'browser', 'iframe', 'service_worker',
                         'shared_worker', 'worker'}
        if (len({t['tabId'] for t in tabs}) != len(tabs)
                or any(t['windowId'] not in windows_by_id for t in tabs if type(t.get('windowId')) is int)
                or any(type(t.get('windowId')) is not int for t in tabs)
                or any(type(w.get('tabCount')) is not int or w['tabCount'] != sum(
                    t['windowId'] == w['windowId'] for t in tabs) for w in windows)
                or any(t['targetId'] not in native_targets
                    or native_targets[t['targetId']]['url'] != t['url']
                    or native_targets[t['targetId']]['type'] != 'page'
                    or any(key in native_targets[t['targetId']] and
                           native_targets[t['targetId']][key] != t[key] for key in ('tabId', 'windowId'))
                    for t in tabs)
                or any(p.get('groupId') is not None and p['groupId'] not in group_ids for p in tabs + pages)):
            raise UpstreamError('Browser inventory sources disagree; preserve the recorded binding')
        for group_id, group in groups_by_id.items():
            members = [t for t in tabs if t.get('groupId') == group_id]
            tab_ids = group.get('tabIds')
            if (type(group.get('windowId')) is not int or group['windowId'] not in windows_by_id
                    or not isinstance(tab_ids, list) or any(type(i) is not int for i in tab_ids)
                    or sorted(tab_ids) != sorted(t['tabId'] for t in members)
                    or any(t['windowId'] != group['windowId'] for t in members)):
                raise UpstreamError('Browser group membership disagrees; preserve the recorded binding')
        identity_keys = ('tabId', 'groupId', 'windowId', 'url')
        for page in pages:
            tab = physical.get(page['targetId'])
            if not tab or any(page.get(key) != tab.get(key) for key in identity_keys):
                raise UpstreamError('SDK and native physical tab identities disagree; preserve the recorded binding')
        old_group = selection['group_id']
        old_targets = set(selection['target_ids'])
        prior = [p for p in record['pages'] + record.get('prior_bindings', []) if p.get('groupId') == old_group]
        prior_urls = {p['url'] for p in prior}
        prior_tabs = {p['tabId'] for p in prior}
        if (old_group in group_ids or old_targets.intersection(ids)
                or any(t.get('tabId') in prior_tabs for t in targets + tabs)
                or (not abandon and any(p['url'] in prior_urls for p in targets + tabs + pages))):
            # A browser tab can survive a renderer replacement, group move and
            # navigation. Older records lack a browser generation, so even an
            # ambiguous tab-ID match is evidence to preserve, never to claim.
            raise UpstreamError('Recorded group, target, or possible restored page still exists; preserve the binding')
        if any(row['targetId'] not in physical and row['type'] not in non_tab_types
               and not (row['type'] == 'browser_ui' and row['url'].startswith('chrome://')
                        and 'tabId' not in row and 'windowId' not in row)
               for row in targets):
            raise UpstreamError('Unmapped browser target may be a restoring tab; preserve the recorded binding')
        counts = {'nativeTabs': len(physical), 'nativeTargets': len(ids), 'nativeGroups': len(group_ids)}
        fingerprint = (
            {i: (g['windowId'], sorted(g['tabIds'])) for i, g in groups_by_id.items()},
            {i: w['tabCount'] for i, w in windows_by_id.items()},
            {i: tuple(t.get(key) for key in ('type', 'subtype', 'url', 'tabId', 'windowId'))
             for i, t in native_targets.items()},
            {t['targetId']: tuple(t.get(key) for key in identity_keys) for t in tabs})
        return counts, fingerprint

    async def stable_retirement_evidence(self, baseline, selection, *, abandon=False, assessment=False):
        if not self.task_state or not self.tool_session:
            raise UpstreamError('Retirement requires a durable task and an existing Neo session')
        if not assessment:
            await self.ensure_ready(allow_start=False)
        observation_session = self.tool_session if assessment else None
        _, first = await self.verify_retirement_absence(
            baseline, selection, abandon=abandon, observation_session=observation_session)
        if await self.guard_endpoint() is False:
            raise UpstreamError('Browser became unavailable during retirement; preserve the recorded binding')
        evidence, latest = await self.verify_retirement_absence(
            baseline, selection, abandon=abandon, observation_session=observation_session)
        if first != latest:
            raise UpstreamError('Browser inventory changed during retirement; preserve the recorded binding')
        return evidence

    async def assess_retirement(self, group_id):
        if not self.task_state:
            raise UpstreamError('Retirement assessment requires a durable task')
        baseline = self.task_state.read_record()
        targets = self.task_state.recorded_group_targets(baseline).get(group_id)
        if not targets:
            raise ValueError('The selected group is not recorded for this task')
        selection = dict(group_id=group_id, revision=baseline.get('state_revision', 0), target_ids=targets)
        self.task_state.retirement_record(group_id, selection['revision'], targets)
        evidence = await self.stable_retirement_evidence(baseline, selection, assessment=True)
        if self.task_state.read_record() != baseline:
            raise UpstreamError('Task record changed during assessment; inspect the latest revision')
        return dict(groupId=group_id, retirementStatus='absent_at_assessment', absenceVerified=evidence,
                    retirementInput=selection, userAuthorizationRequired=True,
                    notice='Ask the user to authorize retirement. Retire rechecks browser inventory and revision.')

    async def retire_task_group(self, message, selection, operation_guard, *, abandon=False):
        if not self.task_state:
            raise UpstreamError('Retirement requires a durable task')
        baseline = self.task_state.retirement_record(
            selection['group_id'], selection['revision'], selection['target_ids'])
        evidence = await self.stable_retirement_evidence(baseline, selection, abandon=abandon)
        revision = self.task_state.archive_group(
            baseline, selection['group_id'], set(selection['target_ids']), operation_guard, abandon=abandon)
        self.bind_task(self.task_state.task_id)
        value = {'previousRevision': selection['revision'], 'stateRevision': revision,
                 'freshInventoryRequired': True}
        if abandon:
            value.update(abandonedGroupId=selection['group_id'], recordedIdentitiesAbsent=evidence,
                         browserTabsChanged=False,
                         notice='Existing tabs were preserved and were not claimed. Reinspect before opening a tab.')
        else:
            value.update(retiredGroupId=selection['group_id'], absenceVerified=evidence)
        return {'jsonrpc':'2.0', 'id':message['id'], 'result':{
            'content':[{'type':'text','text':json.dumps(value)}], 'structuredContent':value}}

    async def unpark_binding(self, message, selection, operation_guard):
        if not self.task_state or not self.tool_session:
            raise UpstreamError('Parking requires a durable task and an existing Neo session')
        group_id = selection['group_id']
        targets = set(selection['target_ids'])
        record = self.task_state.read_record()
        if record.get('state_revision', 0) != selection['revision']:
            raise UpstreamError('Task revision changed; inspect the current record before parking')
        await self.ensure_ready(allow_start=False)
        info = await self.inventory()
        if any(page.get('ownership') == 'mine' for page in info['pages']):
            raise UpstreamError('Current session still owns tabs; preserve the parked binding')
        revision = self.task_state.resume_parked_group(record, group_id, targets, operation_guard)
        self.bind_task(self.task_state.task_id)
        value = {'groupId': group_id, 'stateRevision': revision, 'browserTabsChanged': False,
                 'parked': False, 'freshInventoryRequired': True}
        return {'jsonrpc':'2.0', 'id':message['id'], 'result':{
            'content':[{'type':'text','text':json.dumps(value)}], 'structuredContent':value}}

    def require_restored_binding(self, info):
        if self.task_state:
            missing = self.task_state.restored_candidates(info['pages'], info['groups'],
                native_targets=info.get('targets'), observed_session=self.tool_session)
            if missing:
                self.task_prepared = False
                raise UpstreamError('Restored task evidence requires explicit recovery; preserve original binding: '
                                    + self.restored_binding_details(missing))

    @staticmethod
    def restored_binding_details(missing):
        no_match = [group['priorGroupId'] for group in missing if not group['candidates']]
        if not no_match:
            return 'read-only restored candidates: ' + json.dumps(missing)
        return ('No restored candidates for ' + json.dumps(no_match)
                + '. Candidate absence does not prove tab absence. Use assess_retirement with the exact old '
                  'group ID, then retire only after user authorization. '
                  'read-only restored candidates: ' + json.dumps(missing))

    async def prepare_task(self, *, claim=True, recovery=None, allow_expired=False):
        if not self.task_state:
            return
        if not self.task_state.task_id:
            raise UpstreamError('Bind the actual task UUID with workspace_cleanup begin before browser work')
        info = await self.inventory()
        self.task_state.checkpoint_session(self.tool_session)
        tabs_schema = next((tool.get('inputSchema',{}) for tool in self.catalog['catalog']['tools'] if tool.get('name')=='tabs'), {})
        actions = tabs_schema.get('properties',{}).get('action',{}).get('enum',[])
        expired = {r['groupId']:r for r in self.task_state.expired_recovery(info['pages'], info['groups'])}
        candidates, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], recovery=recovery,
            allow_expired=claim and allow_expired and 'reclaim_expired' in actions, native_targets=info.get('targets'))
        if 'reclaim_restored' in actions:
            missing = [item for item in self.task_state.restored_candidates(info['pages'], info['groups'], native_targets=info.get('targets'))
                       if recovery is None or item['priorGroupId'] != recovery['prior_group_id']]
            if missing:
                hint = ('With direct authorization to give up a specific old binding, '
                        'use retire for a group proven absent, or abandon to archive the old binding '
                        'while leaving every current tab untouched. These do not grant tab ownership. ')
                if not any(group['candidates'] for group in missing):
                    hint = 'Previous bindings remain pending; '
                else:
                    hint = 'Explicit human designation required for groups with candidates; ' + hint
                raise UpstreamError(hint + self.restored_binding_details(missing))
        if conflicts or (candidates and not claim):
            raise UpstreamError(f'Prior task tabs require ownership reconciliation: {conflicts}')
        restored = None
        if 'reclaim_restored' in actions:
            unselected = [g for g in candidates if g not in expired
                          and (recovery is None or g != recovery['group_id'])]
            if unselected:
                metadata = [dict(groupId=g, pages=[dict(targetId=p['targetId'],url=p['url'])
                    for p in {p['targetId']:p for p in info['pages'] if p.get('groupId') == g}.values()])
                    for g in unselected]
                raise UpstreamError('Explicit human designation required; read-only restored candidates: ' + json.dumps(metadata))
            if recovery is not None and recovery['group_id'] in candidates:
                restored = self.task_state.restored_recovery(recovery, info['pages'], info['groups'])
        native_recoveries = []
        for group_id in candidates:
            if (recovery is None and 'reclaim_restored' not in actions and not any(
                    p.get('targetId') in {q.get('targetId') for q in self.task_state.record.get('pages', [])}
                    for p in info['pages'] if p.get('groupId') == group_id)):
                # Keep the old protocol usable, but a replacement receipt must
                # follow a real native adoption and identify one prior group.
                members = {p['targetId']:p for p in info['pages'] if p.get('groupId') == group_id}
                originals = self.task_state.record.get('pages', [])
                prior_ids = {p.get('groupId') for p in originals}
                prior_ids = [g for g in prior_ids if
                    sorted(p['url'] for p in originals if p.get('groupId') == g) ==
                    sorted(p['url'] for p in members.values()) and
                    not any(p['targetId'] in {q.get('targetId') for q in info['pages']}
                            for p in originals if p.get('groupId') == g)]
                if len(prior_ids) != 1:
                    raise UpstreamError('Legacy adoption provenance is ambiguous; preserve prior binding')
                native_recoveries.append(dict(prior_group_id=prior_ids[0],group_id=group_id,
                    pages=[dict(targetId=t,url=p['url']) for t,p in members.items()],
                    reason='Exact legacy native adoption completed and revalidated'))
            if restored is not None and group_id == restored['groupId']:
                await self.internal_tool('tabs',dict(action='reclaim_restored',**restored))
                info = await self.inventory()
                expected = {p['targetId']:p['url'] for p in restored['expected']}
                aliases = [p for p in info['pages'] if p.get('targetId') in expected]
                members = {p['targetId']:p['url'] for p in info['pages'] if p.get('groupId') == group_id}
                if (members != expected or any(p.get('ownership') != 'mine'
                        or p.get('groupId') != group_id or p.get('url') != expected[p['targetId']] for p in aliases)):
                    raise UpstreamError('Restored task recovery remains unverified; preserve prior binding')
                continue
            if group_id in expired and 'reclaim_expired' in actions:
                await self.internal_tool('tabs',dict(action='reclaim_expired',**expired[group_id]))
                info = await self.inventory()
                expected = {p['targetId']:p['url'] for p in expired[group_id]['expected']}
                aliases = [p for p in info['pages'] if p.get('targetId') in expected]
                owned = {p['targetId'] for p in aliases if p.get('ownership') == 'mine'}
                if (owned != set(expected) or any(other_owner(p)
                        or p.get('groupId') != group_id or p.get('url') != expected[p['targetId']]
                        for p in aliases)):
                    raise UpstreamError('Expired task recovery remains unverified; preserve prior binding')
                continue
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
            if 'reclaim_expired' in actions:
                raise UpstreamError('No exact original expired-task proof; legacy group takeover is disabled')
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
            remaining, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], recovery=recovery,
                native_targets=info.get('targets'))
            if remaining or conflicts:
                raise UpstreamError('Selected group recovery remains unverified; preserve prior bindings')
        self.task_prepared = True
        self.task_state.save(self.tool_session, info['pages'], info['groups'], native_targets=info.get('targets'), recovery=recovery, native_recoveries=native_recoveries)

    async def remember_task(self, *, info=None):
        if self.task_state and self.task_state.task_id:
            if info is None:
                info = await self.inventory()
            self.require_restored_binding(info)
            candidates, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], native_targets=info.get('targets'))
            if candidates or conflicts:
                self.task_prepared = False
                raise UpstreamError('Task ownership changed after the action; preserve prior bindings and reconcile before continuing')
            if self.execution_id:
                self.task_state.record['execution_id'] = self.execution_id
            self.task_state.record['review_required'] = list(self.cleanup.review_required)
            self.task_state.record['used_tabs'] = list(self.cleanup.used.values())
            self.task_state.save(self.tool_session, info['pages'], info['groups'], self.cleanup.retained.items(), native_targets=info.get('targets'))

    async def start_runtime(self):
        # The launcher verifies publisher/profile and serializes concurrent starts.
        await self.run_runtime_script("start-neo.ps1", "-Background")

    async def run_runtime_script(self, script, *arguments):
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(ROOT / script), *arguments]
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

    async def connect(self, message: dict, *, allow_start=True, preserve_existing=False):
        # A former backend port is not authority for a new transport session.
        self.endpoint = self.canonical_endpoint
        self.session = None
        self.protocol = None
        deadline = time.monotonic() + (min(self.startup_seconds, 10) if preserve_existing else self.startup_seconds)
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
            except (BrowserRecordStale, BrowserGenerationChanged) as error:
                await self.reconcile_endpoint_browser(error)
            except (httpx.TransportError, NotReady, OwnedEndpointTransition) as error:
                if not checked_backend and self.backend_resolver is not None:
                    checked_backend = True
                    backend = await asyncio.to_thread(self.backend_resolver)
                    if backend and backend != self.endpoint:
                        # A verified managed child is already serving the browser.
                        # Recheck readiness there without starting another worker.
                        self.endpoint = backend
                        deadline = min(deadline, time.monotonic() + 10)
                        preserve_existing = True
                        continue
                if not preserve_existing and self.endpoint_guard is not None:
                    try:
                        identity = (True if isinstance(error, OwnedEndpointTransition)
                                    else await self.guard_endpoint())
                    except (BrowserRecordStale, BrowserGenerationChanged) as replaced:
                        await self.reconcile_endpoint_browser(replaced)
                    except OwnedEndpointTransition:
                        identity = True
                    if identity is True or isinstance(identity, tuple):
                        # Even if both listeners subsequently disappear, this
                        # request does not authorize a launch during a handoff.
                        deadline = min(deadline, time.monotonic() + 10)
                        preserve_existing = True
                if not started and not preserve_existing:
                    if not allow_start:
                        raise UpstreamError('Neo is unavailable; reconnect initialization never starts it. '
                                            'No original action was dispatched. For new browser work, call tabs action=list.')
                    await self.starter()
                    started = True
                    checked_backend = False  # The verified child may now exist.
                    self.endpoint = self.canonical_endpoint
                if time.monotonic() >= deadline:
                    if preserve_existing:
                        raise UpstreamError('Neo readiness did not recover after observing an owned runtime. '
                                            'The launcher was not retried; inspect the new tab inventory.') from error
                    raise UpstreamError("neo did not become ready before startup deadline")
                await asyncio.sleep(0.3)

    async def ensure_ready(self, *, allow_start=True):
        async with self.lock:
            if self.initialize is None:
                raise UpstreamError("MCP initialize has not completed")
            self.sequence += 1
            # The managed proxy and child server can both disappear while a
            # previously verified browser is being replaced. Preserve that
            # connection's launch boundary even if resolving the child fails.
            managed_reconnect = (self.browser_generation is not None
                                 and self.endpoint_browser is not None
                                 and self.protocol is not None)
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
            except (BrowserRecordStale, BrowserGenerationChanged) as error:
                await self.reconcile_endpoint_browser(error)
            except (httpx.TransportError, SessionGone, NotReady, OwnedEndpointTransition) as error:
                # No website action has been dispatched. Reinitializing is safe.
                reconnect = dict(self.initialize, id=f"neo-reconnect-{self.sequence}")
                try:
                    await self.connect(reconnect, allow_start=allow_start,
                                       preserve_existing=managed_reconnect or isinstance(error, OwnedEndpointTransition))
                except UpstreamError:
                    if managed_reconnect:
                        self.reset_requires_inventory = True
                    raise
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
            except (EndpointIdentityError, BrowserRecordStale, OwnedEndpointTransition):
                return  # Stop this channel; never poll or send to an unverified owner.
            except (httpx.HTTPError, UpstreamError, ValueError):
                # Readiness is checked before the next request; never restart a tool here.
                pass
            await asyncio.sleep(2)

    def handle(self, message: dict):
        # Capture at submission, before the coroutine can wait behind another
        # request. A later catalog cannot authorize an already queued old action.
        record = (self.task_state.record or {}) if self.task_state else {}
        return self._handle(message, self.reset_requires_inventory, record.get('browser_reset_id'),
                            self.endpoint_epoch, record.get('retirement_id'))

    async def _handle(self, message: dict, inventory_pending, arrival_reset_id,
                      arrival_endpoint_epoch, arrival_retirement_id):
        method = message.get("method")
        sent = False
        delivered_page_inventory = False
        try:
            if method == 'tools/call':
                p = message.get('params') or {}
                if p.get('name') in DISABLED_HELP_TOOLS:
                    result = {'isError': True,
                              'content': [{'type': 'text', 'text': 'NEO_HUMAN_HELP_DISABLED: '
                                  'In-browser help is disabled. Ask the user in chat if a human step is required.'}],
                              'structuredContent': {'code': 'NEO_HUMAN_HELP_DISABLED',
                                                    'requestedActionExecuted': False}}
                    self.emit({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
                    return
                a = p.get('arguments') or {}
                if not (p.get('name') == CLEANUP_TOOL_NAME and a.get('action') in ('begin','outputs')):
                    await self.guard_endpoint(defer_recovery=True)  # Reject foreign owners before task bookkeeping.
            if method == "ping":
                # Transport heartbeat must not reopen a deliberately closed browser.
                result = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
            elif method == "initialize":
                async with self.lock:
                    self.initialize = message
                    value = copy.deepcopy(self.catalog['initialize'])
                    value['protocolVersion'] = message['params']['protocolVersion']
                    upstream_instructions = '\n\n'.join(
                        paragraph for paragraph in value.get('instructions', '').split('\n\n')
                        if not any(name in paragraph for name in DISABLED_HELP_TOOLS))
                    value['instructions'] = RECONNECT_GUIDANCE + HOST_RECONNECT_GUIDANCE + CHAT_HELP_GUIDANCE + 'Workspace rules take precedence over the generic upstream tab advice below: resume prior recorded task tabs before opening any new tab. Reuse existing task tabs, and register actually reused business tabs with workspace_cleanup use/pageIds/reason; check mine plus registered physical targets at completion. Use/defer never grant close rights. ' + upstream_instructions
                    if self.cleanup.task_id:
                        value['instructions'] = f'Workspace task_id={self.cleanup.task_id} is already bound. Reuse prior recorded pages. Before final response call workspace_cleanup check including registered used targets; close disposable mine tabs, retain only specific next use or unsaved/pending work, and verify absence. For retain/release pass pageIds as an integer array, not page_id or pages; retain also requires a concrete reason. Every cleanup call requires task_id and has no session argument. ' + value['instructions']
                    result = {'jsonrpc':'2.0', 'id':message['id'], 'result':value}
            elif method == "notifications/initialized":
                self.initialized = True
                return
            elif method == 'tools/list':
                value = copy.deepcopy(self.catalog['catalog'])
                value['tools'] = [tool for tool in value['tools'] if tool.get('name') not in DISABLED_HELP_TOOLS]
                # Reclaim is an internal ledger-validated recovery operation, not
                # an agent-selected way to acquire arbitrary existing groups.
                for tool in value['tools']:
                    name = tool.get('name')
                    properties = tool.setdefault('inputSchema', {}).setdefault('properties', {})
                    properties['inventory_receipt'] = copy.deepcopy(INVENTORY_PROPERTY)
                    if name == 'read':
                        properties['requireContent'] = copy.deepcopy(READ_CONTENT_PROPERTY)
                    if name == 'pdf':
                        tool['description'] += (' PDF success covers only this page. Before continuing on a '
                                                'different page, verify that page’s expected content separately.')
                    if name == 'tabs':
                        tool['description'] = RECONNECT_GUIDANCE + tool.get('description', '')
                        action = properties.get('action', {})
                        if isinstance(action.get('enum'), list):
                            action['enum'] = [item for item in action['enum'] if item not in (
                                'reclaim','reclaim_expired','reclaim_restored')]
                        properties.pop('expected', None)
                        properties.pop('priorSession', None)
                        properties.pop('priorGroupId', None)
                        properties.pop('prior', None)
                cleanup_tool = copy.deepcopy(CLEANUP_TOOL)
                cleanup_tool['description'] = RECONNECT_GUIDANCE + cleanup_tool['description']
                cleanup_tool['inputSchema']['properties']['inventory_receipt'] = copy.deepcopy(INVENTORY_PROPERTY)
                value['tools'].append(cleanup_tool)
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
                self.active_tools[message['id']] = asyncio.current_task()
                try:
                    action = (message.get('params') or {}).get('arguments', {}).get('action')
                    async with self.tool_lock, self.browser_operation(
                            local_only=action in ('begin','outputs'),
                            exclusive=action in ('retire','abandon','unpark_recovery')) as guard:
                        if (arrival_endpoint_epoch != self.endpoint_epoch and
                                (message.get('params') or {}).get('arguments',{}).get('action') not in ('begin','outputs')):
                            raise UpstreamError('Cleanup was queued before browser replacement; use current tab inventory')
                        result = await self.cleanup_call(message, operation_guard=guard,
                                                         arrival_retirement_id=arrival_retirement_id)
                        delivered_page_inventory = bool((result.get('result', {}).get('structuredContent') or {}).get('freshInventoryProvided'))
                finally:
                    self.active_tools.pop(message['id'], None)
            elif method == "tools/call":
                self.active_tools[message["id"]] = asyncio.current_task()
                try:
                    # Serialize first-call minting and handle replacement; concurrent
                    # requests must not fork this connection's browser identity.
                    async with self.tool_lock, self.browser_operation():
                        if arrival_endpoint_epoch != self.endpoint_epoch:
                            raise UpstreamError('Request was queued before browser replacement; use current tab inventory')
                        supplied_session=((message.get('params') or {}).get('arguments') or {}).get('session')
                        message = self.refresh_reset_binding(message)
                        record = (self.task_state.record or {}) if self.task_state else {}
                        current_reset_id = record.get('browser_reset_id')
                        retirement_id = record.get('retirement_id')
                        if arrival_reset_id != current_reset_id:
                            raise UpstreamError('Request predates browser reset and was not replayed. Task binding was refreshed locally; call tabs action=list and construct a NEW request')
                        requires_receipt = current_reset_id or retirement_id or self.endpoint_epoch
                        params = dict(message.get("params") or {})
                        args = dict(params.get("arguments") or {})
                        require_content = args.pop('requireContent', False) if params.get('name') == 'read' else False
                        if type(require_content) is not bool or (require_content and args.get('format', 'markdown') not in ('markdown', 'text')):
                            raise ValueError('requireContent must be a boolean and requires a markdown/text read')
                        inventory_receipt = args.pop('inventory_receipt', None)
                        if inventory_receipt is not None and (not isinstance(inventory_receipt,str) or not inventory_receipt):
                            raise ValueError('inventory_receipt must be a nonempty string')
                        params['arguments'] = args
                        message = dict(message,params=params)
                        if params.get('name') == 'tabs' and (args.get('action') in (
                                'reclaim','reclaim_expired','reclaim_restored')
                                or any(k in args for k in ('expected','priorSession','priorGroupId','prior','expectedTabId','expectedWindowId'))):
                            raise ValueError('Task recovery is internal; use workspace_cleanup begin to reconcile recorded tabs')
                        requested_session = args.get("session")
                        if "session" in args and (not isinstance(requested_session, str) or not requested_session):
                            raise ValueError('session must be a nonempty current browser handle')
                        inventory_only = (params.get("name") in ("tabs", "windows", "tab_groups")
                                          and args.get("action") == "list"
                                          and set(args) <= {"action", "session", "agentName"})
                        if arrival_retirement_id != retirement_id and not inventory_only:
                            raise UpstreamError('Request predates task retirement; inspect a fresh tabs list and issue a new request')
                        if self.reset_requires_inventory and not inventory_only:
                            # Catalog is returned to this caller, not hidden as
                            # a preflight followed by the old page operation.
                            result = await self.reconnect_inventory(message)
                            self.emit(result)
                            self.reset_requires_inventory = False
                            return
                        if (self.reset_requires_inventory or inventory_pending) and not inventory_only:
                            raise UpstreamError('Fresh caller-visible inventory required after reset: call tabs action=list, select its current page IDs/session, then issue a fresh request. Queued prior requests are not replayed')
                        if (requires_receipt and not inventory_only
                                and (not self.inventory_receipt or inventory_receipt != self.inventory_receipt)):
                            raise UpstreamError('Post-reset request requires inventory_receipt from this connection\'s tabs action=list. Select current page IDs/session and issue a fresh request; old requests are not replayed')
                        if requires_receipt and supplied_session is not None and supplied_session != self.tool_session:
                            if not inventory_only:
                                raise UpstreamError('Retired session hint cannot address current pages. Select current page IDs/session from tabs action=list and issue a fresh request')
                            # Inventories may refresh a known session but a caller
                            # cannot bind one by combining a list with an old hint.
                            args.pop('session', None)
                        elif not requires_receipt and requested_session is not None and requested_session != self.tool_session:
                            self.tool_session = requested_session
                            self.task_prepared = False
                        # Fixed observation tools cannot adopt, navigate or edit.
                        # Arbitrary run/evaluate code still requires reconciliation.
                        observation_only = inventory_only or params.get("name") in (
                            "snapshot", "read", "grep", "diff", "screenshot")
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
                        if self.task_state and self.task_state.record and not self.reset_pending:
                            self.task_state.record['browser_closed'] = False
                        if self.task_state and not self.task_state.task_id:
                            raise UpstreamError('Bind the actual task UUID with workspace_cleanup begin before browser work')
                        if self.task_state:
                            self.task_state.mark_execution(self.execution_id)
                        await self.ensure_ready()
                        # Inventory observes conflicts without claiming groups or
                        # replacing the saved task's previous physical bindings.
                        if not observation_only:
                            await self.prepare_task()
                            if self.task_state and self.task_state.parked_groups(self.task_state.record):
                                raise UpstreamError('A legacy parked binding must return to recovery before browser mutations')
                        request, headers = self.tool_request(message)
                        if self.task_state and not observation_only:
                            # Advance durable state before a mutation-capable
                            # dispatch. Administrator close-all CAS must detect
                            # new work even if it later returns to the same tabs.
                            self.task_state._write_record()
                        sent = True
                        existing_outputs, baseline_error = None, None
                        if self.task_state:
                            try:
                                existing_outputs = self.task_state.output_baseline(params.get('name'))
                            except Exception as error:
                                baseline_error = error
                        dispatched_ns = time.time_ns()
                        try:
                            result = await self.post(request, headers=headers)
                        except (BrowserRecordStale, BrowserGenerationChanged) as error:
                            # post detected the change before opening its HTTP
                            # stream. Retire the stale handles on this request.
                            sent = False
                            await self.reconcile_endpoint_browser(error)
                        handle = ((result or {}).get("result", {}).get("_meta") or {}).get(SESSION_META_KEY)
                        if isinstance(handle, str) and handle:
                            self.tool_session = handle
                            self.consume_reset_receipt()
                        upstream = (result or {}).get("result", {})
                        apply_result_guidance(params.get('name'), upstream, require_content)
                        if (inventory_only and params.get('name')=='tabs' and isinstance(result,dict)
                                and 'result' in result and 'error' not in result and not upstream.get('isError')
                                and any(c.get('type')=='text' and c.get('text','').strip() for c in upstream.get('content',[]))):
                            delivered_page_inventory = True
                            if requires_receipt:
                                if self.inventory_receipt is None:
                                    self.inventory_receipt = str(uuid.uuid4())
                                upstream.setdefault('_meta', {})[INVENTORY_META_KEY] = self.inventory_receipt
                                upstream.setdefault('content', []).append({'type':'text','text':
                                    f'inventory_receipt: {self.inventory_receipt}. For new post-reset requests, echo this as inventory_receipt and select current page IDs/session from the inventory above. Do not replay old requests.'})
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
                        if not observation_only and not clean_close and result and 'result' in result:
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
            if delivered_page_inventory:
                self.reset_requires_inventory = False
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
                sent = sent and not isinstance(error, (EndpointIdentityError, BrowserRecordStale, OwnedEndpointTransition))
                status = "OUTCOME_UNKNOWN" if sent else "NEO_NOT_READY"
                guidance = ("Request was dispatched and will NOT be replayed. Inspect the actual page/saved result before retrying any action."
                            if sent else "Tool request was NOT dispatched. Fix runtime readiness before invoking the tool again.")
                self.emit({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": f"{status}: {guidance} {detail}"}]}})
            else:
                self.emit({"jsonrpc": "2.0", "id": message["id"],
                           "error": {"code": -32000, "message": detail}})

    async def cleanup_call(self, message, *, operation_guard=None, arrival_retirement_id=None):
        args = (message.get("params") or {}).get("arguments") or {}
        if not isinstance(args,dict):
            raise ValueError('workspace_cleanup arguments must be an object')
        args = dict(args)
        inventory_receipt = args.pop('inventory_receipt', None)
        if inventory_receipt is not None and (not isinstance(inventory_receipt,str) or not inventory_receipt):
            raise ValueError('inventory_receipt must be a nonempty string')
        schema = CLEANUP_TOOL['inputSchema']
        if (not isinstance(args, dict) or set(args) - schema['properties'].keys()
                or any(key not in args for key in schema['required'])):
            raise ValueError('workspace_cleanup requires action and task_id. For retain/release use pageIds=[integer pageId]; retain requires reason. Use the tool schema for other action-specific arguments. No session argument. No task state or browser was changed.')
        if 'condition' in args and args.get('action') != 'defer':
            raise ValueError('condition is accepted only by defer')
        if 'recovery' in args and args.get('action') != 'resume':
            raise ValueError('Explicit recovery is accepted only by resume')
        observation_actions = ('inventory', 'assess_retirement')
        if 'group_id' in args and args.get('action') not in observation_actions:
            raise ValueError('group_id is accepted only by inventory or assess_retirement')
        action = args.get('action')
        group_actions = {'retire': 'retirement', 'abandon': 'abandonment', 'unpark_recovery': 'parked_binding'}
        if action in group_actions or set(group_actions.values()) & args.keys():
            key = group_actions.get(action)
            selection = args.get(key)
            if (key is None or set(args) != {'action', 'task_id', key}
                    or not isinstance(selection, dict)
                    or set(selection) != {'group_id', 'revision', 'target_ids', 'confirmed'}
                    or selection['confirmed'] is not True
                    or not isinstance(selection['group_id'], str) or not selection['group_id'].strip()
                    or type(selection['revision']) is not int or selection['revision'] < 0
                    or not isinstance(selection['target_ids'], list) or not selection['target_ids']
                    or any(not isinstance(t, str) or not t.strip() for t in selection['target_ids'])
                    or len(set(selection['target_ids'])) != len(selection['target_ids'])):
                raise ValueError('Group binding changes require user confirmation, an exact group ID, revision, and unique prior target IDs')
            self.bind_task(args['task_id'], refresh_scope=True)
            if action in ('retire', 'abandon'):
                return await self.retire_task_group(message, selection, operation_guard, abandon=action == 'abandon')
            return await self.unpark_binding(message, selection, operation_guard)
        if action in observation_actions and (
                set(args) != {'action', 'task_id', 'group_id'}
                or not isinstance(args['group_id'], str) or not args['group_id'].strip()):
            raise ValueError(f'{action} accepts only action, task_id and a nonempty exact group_id')
        self.bind_task(args.get("task_id"), refresh_scope=args.get('action') not in ('begin', 'outputs'))
        current = (self.task_state.record or {}) if self.task_state else {}
        if (arrival_retirement_id != current.get('retirement_id')
                and action not in ('begin', 'outputs', 'check', *observation_actions)):
            raise UpstreamError('Cleanup predates task retirement; inspect a fresh tabs list and issue a new request')
        if action == 'assess_retirement' and (self.reset_requires_inventory or self.reset_pending):
            raise UpstreamError('Retirement assessment requires a fresh inventory; call tabs action=list separately')
        if self.reset_requires_inventory and action not in ('begin','outputs','check'):
            return await self.reconnect_inventory(message)
        recorded_reset = current.get('browser_reset_id') or current.get('retirement_id')
        if (self.endpoint_epoch or recorded_reset) and args.get('action') not in ('begin','outputs','check'):
            if not self.inventory_receipt or inventory_receipt != self.inventory_receipt:
                raise UpstreamError('Post-reset cleanup requires inventory_receipt from this connection\'s tabs action=list; use current page IDs and issue a fresh request')
        if action in observation_actions:
            # Only fixed observations bypass reconciliation.
            if action == 'assess_retirement':
                value = await self.assess_retirement(args['group_id'])
            else:
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
            self.reset_requires_begin = False
            record = self.task_state.read_record() if self.task_state else {}
            value = {'taskBound':True, 'browserStarted':False, 'priorPages':record.get('pages',[]), 'resumeRequired':bool(record.get('pages')), 'priorUsedTabs':record.get('used_tabs',[]),
                     'stateRevision':record.get('state_revision', 0),
                     'recordedGroupTargets': self.task_state.recorded_group_targets(record) if self.task_state else {}}
            if parked := self.task_state.parked_groups(record) if self.task_state else {}:
                value['parkedGroupTargets'] = {g: receipt['targetIds'] for g, receipt in parked.items()}
            if record.get('browser_reset_pending'):
                value.update(browserReset=True, priorHandlesRetired=True)
            if self.reset_requires_inventory:
                value.update(freshInventoryRequired=True,nextStep='Call tabs action=list; select its current page IDs/session before issuing a fresh page request')
            return {'jsonrpc':'2.0','id':message['id'],'result':{'content':[{'type':'text','text':json.dumps(value)}], 'structuredContent':value}}
        if args.get('action') == 'resume':
            if self.task_state:
                self.task_state.mark_execution(self.execution_id)
            await self.ensure_ready()
            self.task_prepared = False
            await self.prepare_task(recovery=args.get('recovery'), allow_expired=True)
            args = dict(args, action='check')
        if args.get("action") not in ("check", "retain", "release", "use", "defer"):
            raise ValueError("Unsupported cleanup action")
        if args.get('action') in ('use','defer'):
            if not self.task_state or not self.tool_session:
                raise ValueError('use/defer require a durable bound task and already-running browser session')
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
        if args.get('action') == 'check' and self.task_state:
            parked = self.task_state.parked_groups(self.task_state.record)
            if parked:
                value['parkedRecoveryGroupIds'] = sorted(parked)
        return {"jsonrpc": "2.0", "id": message["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "structuredContent": value}}

    async def review_task_tabs(self, args):
        """Inspect an existing session only; no readiness startup or action replay."""
        pending_reconciliation = []
        if self.task_state:
            # Observation and preservation must remain possible during an
            # ownership conflict. Inspect it without claiming or overwriting
            # the original physical bindings; never call prepare_task here.
            info = await self.inventory()
            self.require_restored_binding(info)
            candidates, conflicts = self.task_state.reconciliation(info['pages'], info['groups'], native_targets=info.get('targets'))
            pending_reconciliation = conflicts + [dict(groupId=gid,
                reason='Recorded task group requires explicit resume') for gid in candidates]
            if pending_reconciliation:
                self.task_prepared = False
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
        final_info = None
        if self.task_state:
            # Finish every observation before changing retention/used scope or
            # completion flags. A later snapshot can reveal a replaced browser.
            final_info = await self.inventory()
            if scope_session != self.tool_session:
                raise UpstreamError('Browser session changed during cleanup; preserve original binding')
            self.require_restored_binding(final_info)
            candidates, conflicts = self.task_state.reconciliation(final_info['pages'], final_info['groups'], native_targets=final_info.get('targets'))
            pending_reconciliation = conflicts + [dict(groupId=gid,
                reason='Recorded task group requires explicit resume') for gid in candidates]
        pages = final_info['pages'] if final_info is not None else physical['pages'] if physical else structured.get('value')
        value = self.cleanup.review(self.tool_session, pages, args,
            physical_tabs=physical.get('tabs') if physical else None, targets=physical.get('targets') if physical else None)
        if pending_reconciliation:
            value['pendingReconciliationGroups'] = pending_reconciliation
            value['cleanupStatus'] = 'reconciliation_required'
            if args.get('action') == 'check':
                reason = 'Prior task ownership is unresolved; preserve tabs and inspect pendingReconciliationGroups.'
                if args.get('stop_hook_active') is True:
                    value.pop('decision', None)
                    value.pop('reason', None)
                    value['systemMessage'] = reason
                else:
                    value.update(decision='block', reason=reason)
        if self.task_state and self.execution_id and args.get('action') in ('use','defer'):
            self.task_state.record.update(execution_id=self.execution_id, execution_incomplete=True)
        if self.task_state and self.execution_id and args.get('action') == 'check':
            self.task_state.record['finish_checked_execution'] = (
                self.execution_id if not has_pending_cleanup(value) else None)
        if self.task_state and args.get('action') == 'check' and not has_pending_cleanup(value):
            self.task_state.record['execution_incomplete'] = False
        if pending_reconciliation:
            self.task_state.record['review_required'] = list(self.cleanup.review_required)
            self.task_state.record['retained'] = list(self.cleanup.retained.items())
            self.task_state.save_used(self.cleanup.used.values())
        else:
            await self.remember_task(info=final_info)
        if (args.get("action") == "check" and not has_pending_cleanup(value)
                and not value["retained"] and not self.cleanup.used):
            self.cleanup_empty_session = self.tool_session
        return value

    async def close(self):
        if self.event_task:
            self.event_task.cancel()
            await asyncio.gather(self.event_task, return_exceptions=True)
        await self.client.aclose()


async def main(plan_path=ROOT / "runtime-plan.json"):
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    started_at = datetime.now(timezone.utc).isoformat()

    def record_exit(reason):
        record_bridge_exit(Path(plan['server_state_root']) / 'logs', args.client,
                           bridge.cleanup.task_id, started_at, reason, len(bridge.active_tools))

    def emit(message):
        try:
            print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)
        except OSError:
            # No JSON-RPC response can reach the host. Stop once even if its
            # input pipe is still open; waiting for that pipe can strand us.
            try:
                record_exit('host_stdout_write_failed')
            finally:
                os._exit(74)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--task-id', default=os.environ.get('WORKSPACE_NEO_TASK_ID') or os.environ.get('CODEX_THREAD_ID'))
    parser.add_argument('--client', choices=('codex','agy'), default='codex')
    args = parser.parse_args()
    catalog = json.loads((ROOT / 'official-tools.json').read_text(encoding='utf-8'))
    if catalog.get('server_sha256') != plan['server_override']['sha256']:
        raise UpstreamError('Official schema catalog does not match the approved server; refresh it without launching a browser')
    from neo_endpoint_identity import transport_browser_identity
    bridge = Bridge(plan["mcp_endpoint"], emit, startup_seconds=55, catalog=catalog,
                    endpoint_guard=lambda endpoint: transport_browser_identity(plan, endpoint),
                    state_root=Path(plan['ledger_root']) / 'task-tabs' / args.client, task_id=args.task_id)
    loop = asyncio.get_running_loop()
    if sys.platform == 'win32' and isinstance(loop, GuardedProactorEventLoop):
        loop.on_wakeup_eof = lambda: record_exit('windows_loop_wakeup_eof')
    pending: set[asyncio.Task] = set()
    try:
        while True:
            try:
                line = await asyncio.to_thread(sys.stdin.buffer.readline)
            except OSError:
                record_exit('host_stdin_read_failed')
                raise
            if not line:
                record_exit('host_stdin_eof')
                break
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
        if sys.platform == 'win32' and isinstance(loop, GuardedProactorEventLoop):
            loop.on_wakeup_eof = None


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
