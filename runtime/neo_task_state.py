"""Durable task bindings; never infer ownership from a URL or group title alone."""
import json
import re
import stat
import time
import uuid
from collections import Counter
from pathlib import Path
from workspace_storage import atomic_write_json, ensure_no_reparse_components
from neo_tab_identity import credential_bearing_url, other_owner


class TaskState:
    def __init__(self, root, *, output_root=None):
        self.root = Path(root)
        self.task_id = None
        self.record = None
        self.output_root = Path(output_root) if output_root is not None else None

    def bind(self, task_id):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError("Use the actual lowercase task UUID")
        if self.task_id and self.task_id != task_id:
            raise ValueError("Connection already belongs to another task")
        self.task_id = task_id
        path = self.root / (task_id + '.json')
        ensure_no_reparse_components(path)
        if self.record is None:
            self.record = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                'schema_version': 1, 'task_id': task_id, 'session': None, 'pages': [], 'groups': [], 'retained': []}
            if self.record.get('schema_version') != 1 or self.record.get('task_id') != task_id:
                raise ValueError("Invalid/future task binding; preserve it for inspection")
        return self.record

    def save(self, session, pages, groups, retained=None):
        if not self.task_id:
            return
        # Only actual native mine ownership can create a binding. Keep enough
        # context to restore a group, never page contents or credentials.
        mine = {p['targetId']: p for p in pages if p.get('ownership') == 'mine' and p.get('targetId')}
        keys = ('targetId', 'tabId', 'groupId', 'windowId', 'url')
        selected = [{k:p[k] for k in keys if k in p} for p in mine.values()]
        for page in selected:
            if credential_bearing_url(page['url']):
                raise ValueError('Credential-bearing URL cannot be stored; preserve existing binding')
        group_ids = {p.get('groupId') for p in selected}
        matched = [g for g in groups if g.get('id', g.get('groupId')) in group_ids]
        self.record.update(session=session, pages=selected, groups=matched)
        if retained is not None:
            self.record['retained'] = list(retained)
        self._write_record()

    def save_used(self, used):
        """Persist explicit business scope even if later ownership bookkeeping fails."""
        updated=dict(self.record, used_tabs=list(used), browser_closed=False)
        self._write_record(updated)
        self.record=updated

    def _write_record(self, record=None):
        target = self.root / (self.task_id + '.json')
        ensure_no_reparse_components(target)
        atomic_write_json(target, self.record if record is None else record, backup_count=0)

    def checkpoint_session(self, session):
        if self.task_id and session and self.record.get('session') != session:
            self.record['session'] = session
            self._write_record()

    def mark_execution(self, execution_id):
        if execution_id and self.task_id:
            self.record.update(execution_id=execution_id, execution_incomplete=True)
            self._write_record()

    def _output_directory(self):
        if self.output_root is None:
            plan = json.loads((Path(__file__).parent / 'runtime-plan.json').read_text(encoding='utf-8'))
            self.output_root = Path(plan['server_state_root']) / 'tool-output'
        if not self.output_root.is_absolute():
            raise ValueError('Tool output root must be absolute')
        ensure_no_reparse_components(self.output_root)
        return self.output_root.resolve(strict=False)

    def _output_path(self, raw, producer):
        if not isinstance(raw, str) or producer not in ('evaluate', 'evaluate_in_page', 'read', 'diff', 'snapshot', 'grep'):
            raise ValueError('Unproven tool output path')
        path = Path(raw)
        extension = 'txt' if producer in ('evaluate', 'evaluate_in_page', 'grep') else ('(?:md|txt)' if producer == 'read' else 'md')
        match = re.fullmatch(producer + r'-(\d{13})-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.' + extension, path.name)
        if not path.is_absolute() or not match:
            raise ValueError('Not a generated text output filename')
        directory = self._output_directory()
        ensure_no_reparse_components(path)
        # Rust canonicalize uses Windows extended-length paths (\\?\D:\...),
        # while the plan normally uses D:\.... Compare directory identity after
        # rejecting reparse components, not the spelling of the same directory.
        if not path.parent.samefile(directory):
            raise ValueError('Tool output is outside the exact runtime directory')
        return path, int(match.group(1))

    @staticmethod
    def _output_identity(path):
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Tool output is not a regular file')
        created = getattr(info, 'st_birthtime_ns', None)
        if created is None:
            raise ValueError('Tool output creation time is unavailable')
        return dict(device=info.st_dev, inode=info.st_ino, size_bytes=info.st_size,
                    mtime_ns=info.st_mtime_ns, created_ns=created)

    def output_baseline(self, producer):
        """Names only, within the exact producer directory; never read file bodies."""
        if producer not in ('evaluate', 'evaluate_in_page', 'read', 'diff', 'snapshot', 'grep'):
            return None
        directory = self._output_directory()
        try:
            return frozenset(path.name.casefold() for path in directory.iterdir()
                             if path.name.casefold().startswith(producer + '-'))
        except FileNotFoundError:
            return frozenset()

    def record_output(self, producer, result, dispatched_ns, request_id, session, existing_outputs=None):
        """Claim only direct server spill metadata and a newly created exact file."""
        if not self.task_id or not isinstance(result, dict) or result.get('isError'):
            return False
        fields = result.get('structuredContent')
        if not isinstance(fields, dict) or producer not in ('evaluate', 'evaluate_in_page', 'read', 'diff', 'snapshot', 'grep'):
            return False
        if producer == 'grep':
            proven = (fields.get('truncated') is True and type(fields.get('page')) is int
                      and type(fields.get('count')) is int and fields['count'] > 0
                      and fields.get('over') in ('ax', 'content'))
        else:
            proven = fields.get('writtenToFile') is True and type(fields.get('contentLength')) is int
        if not proven or fields.get('outputWriteFailed'):
            return False
        path, filename_ms = self._output_path(fields.get('path'), producer)
        if existing_outputs is None or path.name.casefold() in existing_outputs:
            raise ValueError('Tool output existed before dispatch or its baseline is unavailable; ownership unverified')
        identity = self._output_identity(path)
        observed_ns = time.time_ns()
        start_ms, end_ms = dispatched_ns // 1_000_000, observed_ns // 1_000_000
        # NTFS kernel timestamps can lag the precise Python clock by <1ms.
        # Pre-dispatch names prevent an older file from exploiting that tolerance.
        if (not start_ms <= filename_ms <= end_ms
                or any(stamp // 1_000_000 < start_ms - 1 or stamp > observed_ns
                       for stamp in (identity['created_ns'], identity['mtime_ns']))):
            raise ValueError('Tool output predates dispatch or has a future timestamp; ownership unverified')
        entry = dict(path=str(path), producer=producer, request_id=request_id,
                     session=session, dispatched_ns=dispatched_ns, observed_ns=observed_ns,
                     identity=identity, retention_reason='Unclassified task output; inspect ongoing or follow-up need before disposal')
        updated = dict(self.record)
        entries = list(updated.get('outputs', []))
        if any(item.get('path') == entry['path'] for item in entries):
            raise ValueError('Output path already recorded; preserve its original provenance')
        entries.append(entry)
        updated['outputs'] = entries
        self._write_record(updated)
        self.record = updated
        return True

    def outputs(self):
        """Local read-only reconciliation; absence or replacement never means completion."""
        rows = []
        for saved in (self.record or {}).get('outputs', []):
            row = dict(saved)
            try:
                path, _ = self._output_path(saved.get('path'), saved.get('producer'))
                current = self._output_identity(path)
                row['status'] = 'present' if current == saved.get('identity') else 'identity_changed'
                row['current_size_bytes'] = current['size_bytes']
            except FileNotFoundError:
                row['status'] = 'absent'
            except Exception as error:
                row.update(status='unverified', diagnostic=f'{type(error).__name__}: {error}')
            rows.append(row)
        return dict(outputs=rows, present_bytes=sum(row['current_size_bytes'] for row in rows if row['status'] == 'present'),
                    automatic_post_action_coverage='untracked: upstream snapshot/diff post-actions omit file metadata',
                    historical_output_coverage='untracked: no retroactive ownership inference',
                    disposal='No files deleted. Inspect exact outputs and preserve follow-up/export needs; absence is not task completion.')

    def recovery_group(self, recovery, pages, groups):
        """Validate an operator-selected group; never derive approval from a title."""
        if (not isinstance(recovery, dict)
                or set(recovery) != {'prior_group_id','group_id','pages','reason'}
                or any(not isinstance(recovery[k],str) or not recovery[k].strip()
                       for k in ('prior_group_id','group_id','reason'))
                or not 8 <= len(recovery['reason'].strip()) <= 240
                or not isinstance(recovery['pages'],list) or not recovery['pages']):
            raise ValueError('Explicit recovery needs prior_group_id, group_id, exact pages and a concrete user-selection reason')
        expected={}
        for item in recovery['pages']:
            if (not isinstance(item,dict) or set(item) != {'targetId','url'}
                    or any(not isinstance(item[k],str) or not item[k] for k in ('targetId','url'))
                    or item['targetId'] in expected):
                raise ValueError('Recovery pages must contain unique exact targetId/url pairs')
            if credential_bearing_url(item['url']):
                raise ValueError('Credential-bearing recovery URL cannot be stored')
            expected[item['targetId']]=item['url']
        old=[p for p in (self.record or {}).get('pages',[]) if p.get('groupId')==recovery['prior_group_id']]
        if not old or sum(g.get('id',g.get('groupId'))==recovery['group_id'] for g in groups)!=1:
            raise ValueError('Recovery needs a recorded prior group and exactly one current selected group')
        members=[p for p in pages if p.get('groupId')==recovery['group_id']]
        if (not members or any(not p.get('targetId') for p in members)
                or {p['targetId']:p.get('url') for p in members} != expected
                or any(expected.get(p['targetId']) != p.get('url') for p in members)):
            raise ValueError('Selected recovery membership or URL changed; inspect and confirm again')
        aliases=[p for p in pages if p.get('targetId') in expected]
        if any(p.get('groupId') != recovery['group_id'] or p.get('url') != expected[p['targetId']] for p in aliases):
            raise ValueError('Selected physical targets have conflicting aliases; inspect current ownership')
        if any(other_owner(p) for p in aliases):
            raise ValueError('Selected recovery group has another active or unknown owner')
        old_targets={p['targetId'] for p in old}
        if any(p.get('targetId') in old_targets and p.get('groupId')!=recovery['group_id'] for p in pages):
            raise ValueError('Prior physical task tabs are still present elsewhere; preserve both groups')
        return recovery['group_id'], members

    def reconciliation(self, pages, groups, *, recovery=None):
        """Return unique proven groups or unresolved conflicts, without mutations."""
        selected = self.recovery_group(recovery, pages, groups) if recovery is not None else None
        if not self.record or not self.record['pages']:
            return [], []
        physical = {p['targetId']:p for p in pages if p.get('targetId')}
        candidates, unresolved = [], []
        for old_id in {p.get('groupId') for p in self.record['pages']}:
            old = [p for p in self.record['pages'] if p.get('groupId') == old_id]
            if selected and old_id == recovery['prior_group_id']:
                owned={p['targetId'] for p in selected[1] if p.get('ownership')=='mine'}
                if not {p['targetId'] for p in selected[1]}.issubset(owned):
                    candidates.append(selected[0])
                continue
            old_targets={p['targetId'] for p in old}
            if any(p.get('targetId') in old_targets and
                   other_owner(p) for p in pages):
                unresolved.append({'groupId':old_id,'reason':'Another active or unknown owner occupies a recorded physical target'})
                continue
            owned_targets={p['targetId'] for p in pages if p.get('ownership')=='mine' and p.get('targetId')}
            surviving=old_targets.intersection(physical)
            if surviving and surviving.issubset(owned_targets):
                # Complete physical absence proves the other old tabs closed.
                # A still-owned exact target proves task continuity.
                continue
            same_group=[p for p in pages if old_id and p.get('groupId')==old_id]
            if (not surviving and same_group and all(
                    p.get('targetId') in owned_targets and p.get('ownership') in ('mine','user')
                    and (p.get('ownership')!='user' or p.get('ownerAgentId') is None) for p in same_group)):
                # Owned replacement inside the exact saved group: no adoption,
                # title inference, or unowned restored target is involved.
                continue
            titles = [g.get('title') for g in self.record['groups'] if g.get('id', g.get('groupId')) == old_id]
            expected = Counter(p['url'] for p in old)
            hits = []
            related = False
            for group in groups:
                gid = group.get('id', group.get('groupId'))
                same = gid == old_id or (titles and group.get('title') in titles)
                related = related or bool(same)
                members = {p['targetId']:p for p in pages if p.get('groupId') == gid and p.get('targetId')}
                if same and Counter(p['url'] for p in members.values()) == expected:
                    # Preserve every alias's ownership during conflict checks.
                    if any(p.get('ownership') == 'other-agent' for p in pages if p.get('groupId') == gid):
                        unresolved.append({'groupId':old_id,'reason':'Another active owner occupies the recorded group'})
                        continue
                    hits.append((gid, list(members.values())))
            owned_targets = {p['targetId'] for p in pages if p.get('ownership') == 'mine' and p.get('targetId')}
            if len(hits) == 1 and all(p['targetId'] in owned_targets for p in hits[0][1]):
                # Adoption may have completed after a prior failed inspection.
                # The saved full group fingerprint and native mine ownership
                # prove reuse even when restart replaced every target ID.
                continue
            if len(hits) == 1 and all(p.get('ownership') == 'user' and p.get('ownerAgentId') is None for p in hits[0][1]):
                candidates.append(hits[0][0])
            elif hits or related or any(p['targetId'] in physical for p in old):
                unresolved.append({'groupId':old_id, 'reason':'Ownership/membership conflict; no automatic claim'})
            # Actual absence with no matching restored group means closed; a URL
            # elsewhere does not establish this task's ownership.
        return candidates, unresolved
