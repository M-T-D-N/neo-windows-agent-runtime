"""Read-only end-of-turn checks for this bridge's browser ownership.

Never infer task completion from transport expiry, and never close pages here.
A Stop hook requests one agent continuation to inspect and close disposable pages.
"""
from __future__ import annotations
import uuid
import copy
from collections import defaultdict
from neo_tab_identity import credential_bearing_url, other_owner

DEFER_CONDITIONS = ('unsaved', 'request_pending', 'other_owner', 'permission_missing', 'next_use', 'inspection_failed')


def has_pending_cleanup(value):
    return bool(value.get('pendingPageIds') or value.get('reviewPageIds') or value.get('pendingUsedTargetIds'))


def _page_ids(args):
    ids = args.get('pageIds', [])
    if not isinstance(ids, list) or any(type(i) is not int for i in ids):
        raise ValueError('pageIds must be integer IDs')
    return ids


def _aliases_by_target(pages):
    aliases = defaultdict(list)
    for page in pages:
        aliases[page.get('targetId')].append(page)
    return aliases


TOOL_NAME = "workspace_cleanup"
TOOL = {
    "name": TOOL_NAME,
    "description": "Workspace task tab lifecycle. Bind the actual task UUID with begin locally before browser work. Register actually reused business tabs with use/pageIds/reason before work; business research/reading/review also counts as actual reuse; incidental inventory/status observation of another task must not enroll tabs. Use never grants close rights. Check includes durable used physical targets even outside mine. For registered non-mine tabs use defer/pageIds/condition/reason after fresh inspection when unsaved, pending, actively owned elsewhere, lacking close rights, needed next or inspection failed. Deferred is preserved, not cleaned. Direct raw HTTP bypasses this local lifecycle. Resume explicitly restores uniquely proven recorded task groups through the official native group path; this action may start Neo. Inventory requires group_id and returns current physical metadata for that group without reconciliation, adoption, task-record writes or browser startup; use its recoveryPages in explicit resume recovery. Other-owner metadata remains readable, never claimable by inspection. Check/retain/release/outputs/use/defer/inventory never start Neo. Outputs is a local read-only inventory of proven direct-tool spill files, with exact identity/status/size and retention reason; inspect it at producer closeout and recycle only exact unused files after preserving follow-up/export needs. No output deletion or age expiry is performed. Automatic post-action and historical outputs remain untracked. Before ending work, check remaining task tabs. Close disposable tabs using native Neo tools. Explicitly retain pages only for confirmed next use, unsaved/pending work, or a documented inspection failure; supply a concrete reason. Retention is tied to the exact session, target and URL and accepted for one Stop boundary. Reaffirm its purpose at the next boundary. Other tasks' pages are never claimed or closed. resume recovery is only for a group explicitly selected by the human user when automatic matching fails; supply the exact current targetId/URL membership and prior group. A task or matching title alone is not user approval. Other active owners and changed membership are rejected.",
    "inputSchema": {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["begin", "resume", "check", "retain", "release", "outputs", "use", "defer", "inventory"]},
            "task_id": {"type": "string", "description": "Actual stable Codex task or AGY conversation UUID. Begin binds locally without launching Neo; resume reconciles that task's recorded groups before new tabs."},
            "group_id": {"type": "string", "minLength": 1, "description": "inventory only. Exact current group ID from tab_groups/tabs list; returns physical targetId/URL metadata without adoption or page content."},
            "pageIds": {"type": "array", "items": {"type": "integer"}},
            "reason": {"type": "string"},
            "condition": {"type": "string", "enum": list(DEFER_CONDITIONS)},
            "stop_hook_active": {"type": "boolean"},
            "recovery": {"type":"object", "description":"resume only. Requires explicit human designation of this restored group for this task; never infer it from the task title. Preserves contents and uses native group adoption.",
                "properties": {
                    "prior_group_id":{"type":"string"}, "group_id":{"type":"string"},
                    "reason":{"type":"string","minLength":8,"maxLength":240},
                    "pages":{"type":"array","minItems":1,"items":{"type":"object",
                        "properties":{"targetId":{"type":"string"},"url":{"type":"string"}},
                        "required":["targetId","url"],"additionalProperties":False}}},
                "required":["prior_group_id","group_id","pages","reason"],"additionalProperties":False},
        }, "required": ["action", "task_id"], "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
}

class CleanupPolicy:
    def __init__(self):
        self.task_id = None
        self.retained = {}
        self.review_required = set()
        self.used = {}

    def bind(self, task_id):
        if not isinstance(task_id, str) or str(uuid.UUID(task_id)) != task_id:
            raise ValueError("task_id must be the actual lowercase thread UUID")
        if self.task_id is not None and self.task_id != task_id:
            raise ValueError("This connection is bound to another task; no ownership inference or cleanup is permitted")
        self.task_id = task_id

    def review(self, session, pages, args, *, physical_tabs=None, targets=None):
        self.bind(args.get("task_id"))
        action = args.get("action")
        if action not in ("check", "retain", "release", "use", "defer"):
            raise ValueError("Unsupported cleanup action")
        if not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages):
            raise ValueError("Neo tab listing is unavailable; no successful cleanup can be inferred")
        used_result = self.review_used(session, pages, physical_tabs, targets, args) if self.used or action in ('use','defer') else {}
        if self.used:
            actual={t['targetId']:t for t in physical_tabs}
            pages=[dict(p, url=actual[p['targetId']]['url'])
                   if p.get('targetId') in self.used and p.get('targetId') in actual else p for p in pages]
        # Distinct SDK aliases can describe one physical target. Only native mine
        # ownership authorizes this task; group titles and URL matches do not.
        mine = {}
        aliases = _aliases_by_target(pages)
        for page in pages:
            if page.get("ownership") == "mine" and not (page.get("targetId") in self.used and
                    any(other_owner(p) for p in aliases[page.get("targetId")])):
                target = page.get("targetId")
                if not target or not isinstance(page.get("pageId"), int) or not isinstance(page.get("url"), str):
                    raise ValueError("Owned tab lacks physical identity; inspect it with native Neo tools")
                mine[target] = page
        ids = _page_ids(args)
        selected = [p for p in pages if p.get("ownership") == "mine" and p.get("pageId") in ids]
        if action in ("retain", "release"):
            if not ids or set(ids) != {p["pageId"] for p in selected}:
                raise ValueError("Every requested page must currently belong to this task")
            reason = args.get("reason", "")
            if action == "retain" and (not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 240):
                raise ValueError("Retain requires a concrete reason of 8-240 characters")
            for page in selected:
                key = (session, page["targetId"], page["url"])
                if action == "retain":
                    self.retained[key] = reason.strip()
                    self.review_required.discard(key)
                else:
                    self.retained.pop(key, None)
                    self.review_required.discard(key)
        live = {(session, p["targetId"], p["url"]) for p in mine.values()}
        # Keep another session's decision only while its exact physical target
        # still exists. A complete native inventory proves closed old targets
        # absent; changed URLs in this session require a fresh decision.
        physical = {p.get("targetId") for p in pages}
        self.retained = {k:v for k,v in self.retained.items()
                         if (k[0] == session and k in live)
                         or (k[0] != session and k[1] in physical)}
        self.review_required.intersection_update(self.retained)
        pending = [p["pageId"] for p in mine.values() if (session, p["targetId"], p["url"]) not in self.retained]
        kept = [{"pageId":p["pageId"], "reason":self.retained[(session,p["targetId"],p["url"])]} for p in mine.values() if (session,p["targetId"],p["url"]) in self.retained]
        result = {"pendingPageIds":pending, "retained":kept, **used_result}
        review = [p["pageId"] for p in mine.values()
                  if (session, p["targetId"], p["url"]) in self.review_required]
        if review:
            result["reviewPageIds"] = review
        if action == "check" and (pending or review or result.get("pendingUsedTargetIds")):
            reason = f"Neo used physical targets needing classification {result.get('pendingUsedTargetIds',[])}; use does not grant close authority. Inspect actual document model/rendered text/images and saved result, not blank inputs or invisible spinner counts. Neo tab cleanup remains: disposable/unclassified pages {pending}; previously retained pages needing a fresh purpose review {review}. Inspect current ownership, unsaved input and pending requests. Reaffirm retention only after checking a concrete still-current next use or unresolved condition with action=retain; otherwise release it and close the disposable page. Verify physical absence. A previous Main/worker recovery instruction or a user-labelled SDK alias is not proof of continuing need or user creation. Preserve actual user work and other tasks' tabs. Report unresolved state accurately."
            if args.get("stop_hook_active") is True:
                result["systemMessage"] = reason
            else:
                result.update(decision="block", reason=reason)
        elif action == "check" and type(args.get("stop_hook_active")) is bool:
            # Only a successful actual Stop boundary consumes current approval.
            # Ordinary mid-work checks and blocked cleanup must not invalidate it.
            # Keep the reason and physical identity: expiry requests review, never
            # automatic deletion, including for unsaved and pending work.
            self.review_required.update(live.intersection(self.retained))
            for item in self.used.values():
                if item.get("deferred"):
                    item["deferred"]["review_required"] = True
        return result

    def review_used(self, session, pages, tabs, targets, args):
        previous=copy.deepcopy(self.used)
        try:
            return self._review_used(session,pages,tabs,targets,args)
        except Exception:
            self.used=previous
            raise

    @staticmethod
    def _physical_inventory(tabs, targets):
        if (not isinstance(tabs, list) or not isinstance(targets, list)
                or any(not isinstance(t, dict) or not t.get('targetId') or type(t.get('tabId')) is not int
                       or not isinstance(t.get('url'), str) for t in tabs)
                or len({t['targetId'] for t in tabs}) != len(tabs)
                or any(not isinstance(t, str) for t in targets)):
            raise ValueError('Complete physical tab and target inventory required; preserve used scope')
        return {tab['targetId']: tab for tab in tabs}

    def _used_selection(self, pages, physical, targets, args, ids):
        selected = [p for p in pages if p.get('pageId') in ids]
        reason = args.get('reason', '')
        if (not ids or set(ids) != {p['pageId'] for p in selected} or len(selected) != len(set(ids))
                or not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 240):
            raise ValueError('use/defer require exact current pageIds and concrete reason of 8-240 characters')
        for page in selected:
            current = physical.get(page.get('targetId'))
            if not current or current['targetId'] not in targets or current['tabId'] != page.get('tabId'):
                raise ValueError('Selected alias has no verified current physical identity')
            if credential_bearing_url(current['url']):
                raise ValueError('Credential-bearing URL cannot be stored')
            if args['action'] == 'defer' and page['targetId'] not in self.used:
                raise ValueError('defer only accepts previously registered actual-work targets')
        if args['action'] == 'defer' and args.get('condition') not in DEFER_CONDITIONS:
            raise ValueError('defer requires a concrete unresolved condition')
        return selected

    def _register_used(self, selected, physical, targets, purpose):
        for page in selected:
            current = physical[page['targetId']]
            # Renderer replacement needs explicit current use, not a title/URL guess.
            self.used = {k: v for k, v in self.used.items()
                         if not (v['tabId'] == current['tabId'] and k not in targets)}
            self.used[current['targetId']] = {k: current[k] for k in ('targetId', 'tabId', 'url')}
            self.used[current['targetId']]['purpose'] = purpose

    @staticmethod
    def _used_row(target, saved, current, aliases, targets):
        authority = 'identity_changed'
        if current and current['tabId'] == saved['tabId'] and target in targets:
            if any(other_owner(p) for p in aliases):
                authority = 'other_owner'
            elif any(p.get('ownership') == 'mine' for p in aliases):
                authority = 'native_mine'
            else:
                authority = 'permission_missing' if aliases else 'unresolved_alias'
        if current and credential_bearing_url(current['url']):
            raise ValueError('Credential-bearing current URL cannot enter used task state')
        return dict(targetId=target, tabId=saved['tabId'], url=current['url'] if current else saved['url'],
                    pageIds=sorted({p['pageId'] for p in aliases}), purpose=saved['purpose'],
                    closeAuthority=authority, closeAllowed=authority == 'native_mine', requiresStateInspection=True)

    def _review_used(self, session, pages, tabs, targets, args):
        """Explicit task use is bookkeeping, never adoption or close authorization."""
        physical = self._physical_inventory(tabs, targets)
        action, ids = args['action'], _page_ids(args)
        if action in ('use', 'defer'):
            selected = self._used_selection(pages, physical, targets, args, ids)
            if action == 'use':
                self._register_used(selected, physical, targets, args['reason'].strip())
        aliases_by_target = _aliases_by_target(pages)
        live_tab_ids = {tab['tabId'] for tab in tabs}
        rows, pending, deferred, absent = [], [], [], []
        for target, saved in self.used.items():
            current, aliases = physical.get(target), aliases_by_target[target]
            if not current and target not in targets and saved['tabId'] not in live_tab_ids:
                absent.append(target)
                continue
            row = self._used_row(target, saved, current, aliases, targets)
            owners = sorted({(str(p.get('ownership')), str(p.get('ownerAgentId'))) for p in aliases})
            signature = [session, row['url'], saved['tabId'], row['closeAuthority'], [list(pair) for pair in owners]]
            if action == 'defer' and any(p['pageId'] in ids for p in aliases):
                if row['closeAllowed']:
                    raise ValueError('Native owned targets use retain after state inspection, not defer')
                saved['deferred'] = dict(signature=signature, condition=args['condition'],
                                         reason=args['reason'].strip(), review_required=False)
            decision = saved.get('deferred', {})
            if not row['closeAllowed']:
                if decision.get('signature') == signature and not decision.get('review_required'):
                    deferred.append(dict(targetId=target, condition=decision['condition'], reason=decision['reason']))
                else:
                    pending.append(target)
            rows.append(row)
        for target in absent:
            self.used.pop(target)
        result = dict(usedPages=rows, pendingUsedTargetIds=pending, deferredUsedPages=deferred)
        if deferred:
            result['cleanupStatus'] = 'deferred'
        if absent:
            result['closedUsedTargetIds'] = absent
        return result
