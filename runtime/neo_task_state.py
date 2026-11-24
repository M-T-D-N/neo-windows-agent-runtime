"""Durable task bindings; never infer ownership from a URL or group title alone."""
import json
import re
import stat
import time
import uuid
from collections import Counter
from pathlib import Path
from workspace_storage import atomic_write_json, ensure_no_reparse_components
from workspace_storage.atomic import _file_lock
from neo_tab_identity import credential_bearing_url, other_owner
from neo_operation_boundary import OperationBoundary


class TaskState:
    def __init__(self, root, *, output_root=None):
        self.root = Path(root)
        self.task_id = None
        self.record = None
        self.output_root = Path(output_root) if output_root is not None else None

    def browser_operation(self, *, exclusive=False, timeout=45):
        root=self.root.parent.parent if self.root.parent.name=='task-tabs' else self.root
        return OperationBoundary(root/'browser-operations.lock',exclusive=exclusive,timeout=timeout)

    def task_operation(self):
        return OperationBoundary(self.root / (self.task_id + '.operation'), exclusive=True)

    def bind(self, task_id, *, refresh_scope=False):
        if str(uuid.UUID(task_id)) != task_id:
            raise ValueError("Use the actual lowercase task UUID")
        if self.task_id and self.task_id != task_id:
            raise ValueError("Connection already belongs to another task")
        self.task_id = task_id
        path = self.root / (task_id + '.json')
        ensure_no_reparse_components(path)
        if self.record is not None and path.exists():
            fresh = json.loads(path.read_text(encoding='utf-8'))
            self._validate_record(fresh, task_id)
            reset = fresh.get('browser_reset_id')
            if (refresh_scope and fresh.get('state_revision') != self.record.get('state_revision')
                    or fresh.get('retirement_id') != self.record.get('retirement_id')
                    or reset is not None and (reset != self.record.get('browser_reset_id') or
                    self.record.get('browser_reset_pending') is True and fresh.get('browser_reset_pending') is False)):
                fresh['outputs'] = self._merge_outputs(self.record, fresh)
                self.record = fresh
        if self.record is None:
            fresh = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                'schema_version': 1, 'task_id': task_id, 'session': None, 'pages': [], 'groups': [], 'retained': []}
            self._validate_record(fresh, task_id)
            self.record = fresh
        return self.record

    @classmethod
    def _validate_record(cls, record, task_id):
        if (not isinstance(record, dict) or record.get('task_id') != task_id
                or type(record.get('schema_version')) is not int or record['schema_version'] not in (1, 2, 3)):
            raise ValueError("Invalid/future task binding; preserve it for inspection")
        receipt = record.get('retirement_id')
        history = record.get('retired_bindings')
        if record['schema_version'] in (2, 3):
            if (not isinstance(receipt, str) or str(uuid.UUID(receipt)) != receipt
                    or not isinstance(history, list) or not history
                    or not isinstance(history[-1], dict) or history[-1].get('id') != receipt):
                raise ValueError('Invalid task retirement evidence; preserve it for inspection')
        elif receipt is not None or history is not None:
            raise ValueError('Unexpected retirement evidence on legacy task binding')
        if record.get('browser_reset_id') is not None or record.get('browser_reset_pending'):
            cls._validate_reset(record, task_id)

    @staticmethod
    def _validate_reset(record, task_id):
        reset = record.get('browser_reset_id')
        if (not isinstance(reset, str) or str(uuid.UUID(reset)) != reset
                or record.get('browser_reset_verified') is not True
                or record.get('task_id') != task_id):
            raise ValueError('Invalid administrator reset evidence; preserve task bindings')
        pending = record.get('browser_reset_pending')
        if pending is True:
            if (record.get('browser_closed') is not True or record.get('session') is not None
                    or record.get('pages_session') is not None or any(record.get(k) != [] for k in
                    ('pages','groups','used_tabs','retained','prior_bindings','review_required'))):
                raise ValueError('Incomplete browser reset receipt; preserve task bindings')
        elif pending is not False or not isinstance(record.get('session'),str) or not record['session']:
            raise ValueError('Consumed reset lacks its new session; preserve task bindings')

    @staticmethod
    def _merge_outputs(*records):
        entries = {}
        for record in records:
            for entry in (record or {}).get('outputs', []):
                if not isinstance(entry,dict) or not isinstance(entry.get('path'),str):
                    raise ValueError('Invalid output provenance; preserve task record')
                key = entry['path'].casefold()
                if key in entries and entries[key] != entry:
                    raise ValueError('Conflicting output provenance; preserve both records for inspection')
                entries[key] = entry
        return list(entries.values())

    def save(self, session, pages, groups, retained=None, *, native_targets=None, recovery=None, native_recoveries=()):
        if not self.task_id:
            return
        missing = self.restored_candidates(pages, groups, native_targets=native_targets, observed_session=session)
        selections = ([recovery] if recovery is not None else []) + list(native_recoveries)
        consumed = set()
        for selection in selections:
            if not any(p['priorGroupId'] == selection['prior_group_id'] for p in missing):
                continue
            # Native recovery receipts are supplied only after an actual legacy
            # adoption, never inferred by readonly cleanup from equal URLs.
            group_id, members = self.recovery_group(selection, pages, groups)
            if group_id in consumed:
                raise ValueError('One current group cannot discharge multiple prior bindings')
            consumed.add(group_id)
            expected = {p['targetId'] for p in members}
            aliases = [p for p in pages if p.get('targetId') in expected]
            mine = {p['targetId'] for p in aliases if p.get('ownership') == 'mine'}
            if not expected.issubset(mine) or any(other_owner(p) for p in aliases):
                raise ValueError('Selected recovery is not exclusively native-owned; preserve prior binding')
            missing = [p for p in missing if p['priorGroupId'] != selection['prior_group_id']]
        if missing:
            raise ValueError('Restored task evidence is unresolved; preserve original binding: ' + json.dumps(missing))
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
        self.record.update(session=session, pages_session=session, pages=selected, groups=matched)
        self.record['prior_bindings'] = [p for p in self.record.get('prior_bindings', [])
                                        if p.get('targetId') not in mine]
        if retained is not None:
            self.record['retained'] = list(retained)
        self._write_record()

    def save_used(self, used):
        """Persist explicit business scope even if later ownership bookkeeping fails."""
        updated=dict(self.record, used_tabs=list(used), browser_closed=False)
        self._write_record(updated)
        self.record=updated

    def consume_browser_reset_handle(self, session):
        if not isinstance(session,str) or not session or self.record.get('browser_reset_pending') is not True:
            raise ValueError('Expected a fresh server handle and pending reset')
        updated=dict(self.record,browser_reset_pending=False,browser_closed=False,session=session)
        self._write_record(updated, consume_reset=True)
        self.record=updated

    def _write_record(self, record=None, *, administrator_reset=False, consume_reset=False,
                      retire_binding=False, expected_state=None, operation_guard=None, outputs_only=False):
        if administrator_reset or retire_binding:
            if not isinstance(operation_guard,OperationBoundary):
                raise ValueError('Task administration requires the profile exclusive operation boundary')
            operation_guard.require_exclusive(self.browser_operation().path)
        target = self.root / (self.task_id + '.json')
        ensure_no_reparse_components(target)
        updated = self.record if record is None else record
        # Serialize read/merge/write transactions separately from atomic JSON
        # installation's own lock. Output receipts from concurrent clients must
        # survive every task-state update, including administrator close-all.
        with _file_lock(target.with_suffix('.update'), timeout=5):
            disk = json.loads(target.read_text(encoding='utf-8')) if target.exists() else {}
            if disk:
                self._validate_record(disk, self.task_id)
            self._validate_record(updated, self.task_id)
            if administrator_reset or retire_binding:
                if not isinstance(expected_state,dict) or disk != expected_state:
                    boundary = 'closure verification' if administrator_reset else 'verification'
                    raise ValueError(f'Task changed since {boundary}; preserve current browser bindings')
            if retire_binding:
                if updated.get('retirement_id') == disk.get('retirement_id'):
                    raise ValueError('Task retirement receipt was already issued')
            elif disk.get('retirement_id') != updated.get('retirement_id'):
                raise ValueError('Task retirement changed in another connection; bind before writing')
            if administrator_reset:
                self._validate_reset(updated, self.task_id)
                if updated.get('browser_reset_pending') is not True:
                    raise ValueError('Administrator reset must start with a closed pending receipt')
                if disk.get('browser_reset_id') == updated.get('browser_reset_id'):
                    raise ValueError('Reset receipt was already issued; verify a new closure boundary')
            elif disk.get('browser_reset_id') != updated.get('browser_reset_id') or (
                    disk.get('browser_reset_pending') is False and updated.get('browser_reset_pending') is True):
                raise ValueError('Reset changed in another connection; bind before writing')
            elif consume_reset and disk.get('browser_reset_pending') is False and disk.get('session') != updated.get('session'):
                raise ValueError('Reset was consumed by another connection; bind before continuing')
            revision = disk.get('state_revision',0)
            if type(revision) is not int or revision < 0:
                raise ValueError('Invalid task revision; preserve current state')
            outputs = self._merge_outputs(disk, self.record, updated)
            if outputs_only:
                # An output receipt cannot write a cached page binding back.
                stored = dict(disk or updated, outputs=outputs, state_revision=revision+1)
                atomic_write_json(target, stored, backup_count=0)
                updated['outputs'] = outputs
                if updated.get('state_revision',0) == revision:
                    updated['state_revision'] = revision+1
                return
            if expected_state is None and revision != updated.get('state_revision',0):
                raise ValueError('Task scope changed in another connection; inspect current inventory')
            updated['outputs'] = outputs
            updated['state_revision'] = revision + 1
            atomic_write_json(target, updated, backup_count=0)

    def read_record(self):
        """Read a validated disk snapshot without refreshing cached page authority."""
        target = self.root / (self.task_id + '.json')
        ensure_no_reparse_components(target)
        if target.exists():
            record = json.loads(target.read_text(encoding='utf-8'))
        elif self.record and not self.record.get('state_revision'):
            record = self.record
        else:
            raise ValueError('Persisted task record is missing; inspect it before continuing')
        self._validate_record(record, self.task_id)
        return record

    @staticmethod
    def recorded_group_targets(record):
        by_group = {}
        for page in record.get('pages', []) + record.get('prior_bindings', []):
            if page.get('groupId') and page.get('targetId'):
                by_group.setdefault(page['groupId'], set()).add(page['targetId'])
        return {group: sorted(targets) for group, targets in by_group.items()}

    def retirement_record(self, group_id, expected_revision, target_ids):
        """Take the exact disk revision before collecting browser evidence."""
        record = self.read_record()
        if record.get('state_revision', 0) != expected_revision:
            raise ValueError('Task revision changed; inspect the current record before retirement')
        pages = [p for p in record['pages'] if p.get('groupId') == group_id]
        history = [p for p in record.get('prior_bindings', []) if p.get('groupId') == group_id]
        if (not pages or {p['targetId'] for p in pages + history} != set(target_ids)
                or any(not isinstance(p.get('url'), str) or not p['url'].strip()
                       or type(p.get('tabId')) is not int for p in pages + history)
                or any(p.get('targetId') in target_ids and p.get('groupId') != group_id
                       for p in record['pages'] + record.get('prior_bindings', []))):
            raise ValueError('Recorded group membership is different or ambiguous; preserve prior bindings')
        return record

    @staticmethod
    def parked_groups(record):
        receipts = {item['groupId']: item for item in record.get('retired_bindings', [])
                    if item.get('disposition') in ('parked', 'resumed')}
        return {group: item for group, item in receipts.items() if item['disposition'] == 'parked'}

    def archive_group(self, record, group_id, target_ids, operation_guard, *, abandon=False):
        """Archive only the approved prior binding in the same CAS transaction."""
        archived = {}
        updated = dict(record)
        belongs = {
            'pages': lambda p: p.get('groupId') == group_id,
            'groups': lambda g: g.get('id', g.get('groupId')) == group_id,
            'prior_bindings': lambda p: p.get('groupId') == group_id,
            'used_tabs': lambda p: p['targetId'] in target_ids,
            'retained': lambda entry: entry[0][1] in target_ids,
            'review_required': lambda key: key[1] in target_ids,
        }
        for name, selected in belongs.items():
            if name in record:
                archived[name] = [row for row in record[name] if selected(row)]
                updated[name] = [row for row in record[name] if not selected(row)]
        receipt_id = str(uuid.uuid4())
        receipt = dict(id=receipt_id, groupId=group_id, targetIds=sorted(target_ids),
                       revision=record.get('state_revision', 0), removed=archived,
                       session=record.get('session'), pages_session=record.get('pages_session'))
        if abandon:
            receipt['disposition'] = 'abandoned'
        # Legacy bridges already refuse to write a schema they cannot read.
        # Only a ledger with an explicit retirement moves to the new schema.
        updated.update(schema_version=record['schema_version'] if record['schema_version'] == 3 else 2, retirement_id=receipt_id,
                       retired_bindings=[*record.get('retired_bindings', []), receipt])
        self._write_record(updated, retire_binding=True, expected_state=record, operation_guard=operation_guard)
        return updated['state_revision']

    def resume_parked_group(self, record, group_id, target_ids, operation_guard):
        receipt = next((item for item in reversed(record.get('retired_bindings', []))
                        if item.get('groupId') == group_id), None)
        if (not receipt or receipt.get('disposition') != 'parked'
                or receipt['targetIds'] != sorted(target_ids) or record['pages']):
            raise ValueError('Parked membership changed or the current task still owns tabs; preserve the parked binding')
        updated = dict(record, pages_session=receipt['pages_session'])
        for name, rows in receipt['removed'].items():
            current = record.get(name, [])
            if name not in ('pages', 'groups', 'prior_bindings', 'used_tabs', 'retained', 'review_required'):
                raise ValueError('Unsupported parked scope; preserve the record')
            if name == 'used_tabs' and {p['targetId'] for p in current} & set(target_ids):
                raise ValueError('Active work is tracking the parked target; preserve both scopes')
            updated[name] = [*current, *(row for row in rows if row not in current)]
        resumed = dict(id=str(uuid.uuid4()), groupId=group_id, targetIds=sorted(target_ids),
                       revision=record.get('state_revision', 0), disposition='resumed')
        updated.update(retirement_id=resumed['id'], retired_bindings=[*record['retired_bindings'], resumed])
        self._write_record(updated, retire_binding=True, expected_state=record, operation_guard=operation_guard)
        return updated['state_revision']

    def checkpoint_session(self, session):
        if self.task_id and session and self.record.get('session') != session:
            owner_session = self.record.get('pages_session')
            if owner_session and owner_session != session:
                prior = {p['targetId']:p for p in self.record.get('prior_bindings', [])}
                for page in self.record.get('pages', []):
                    prior[page['targetId']] = dict(page, session=owner_session)
                self.record['prior_bindings'] = list(prior.values())
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
        self._write_record(updated, outputs_only=True)
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

    def _has_preexisting_sibling(self, group_id, old, pages):
        """Blocking evidence only; a known non-owned sibling is not a restore."""
        saved = [g for g in self.record.get('groups', []) if g.get('id', g.get('groupId')) == group_id]
        if len(saved) != 1 or not isinstance(saved[0].get('tabIds'), list):
            return False
        owned_ids = {p.get('tabId') for p in old if type(p.get('tabId')) is int}
        siblings = {t for t in saved[0]['tabIds'] if type(t) is int} - owned_ids
        known_targets = {p.get('targetId') for p in self.record.get('used_tabs', [])
                         if type(p.get('tabId')) is int and p['tabId'] in siblings}
        return bool(siblings) and any(p.get('groupId') == group_id and (
            p.get('tabId') in siblings or p.get('targetId') in known_targets) for p in pages)

    def _registered_unclaimed_remainder(self, group_id, old, pages, groups, native_targets):
        """Prove owned parents closed without adopting their already-used siblings."""
        if (not isinstance(native_targets, list)
                or any(not isinstance(t, str) or not t for t in native_targets)
                or any(p['targetId'] in native_targets for p in old)):
            return False
        saved = [g for g in self.record.get('groups', []) if g.get('id', g.get('groupId')) == group_id]
        current = [g for g in groups if g.get('id', g.get('groupId')) == group_id]
        if not group_id or len(saved) != 1 or len(current) != 1:
            return False
        for group in (saved[0], current[0]):
            ids = group.get('tabIds')
            if (not isinstance(ids, list) or any(type(t) is not int for t in ids)
                    or len(ids) != len(set(ids))):
                return False
        if any(type(p.get('tabId')) is not int for p in old):
            return False
        owned_ids = {p['tabId'] for p in old}
        if not owned_ids.issubset(saved[0]['tabIds']):
            return False
        members = [p for p in pages if p.get('groupId') == group_id]
        targets = {p.get('targetId') for p in members}
        used = self.record.get('used_tabs', [])
        if (not members or not targets.issubset(native_targets) or None in targets
                or not isinstance(used, list) or any(not isinstance(p, dict) for p in used)):
            return False
        registered = {p.get('targetId'): p for p in used}
        if len(registered) != len(used):
            return False
        aliases = [p for p in pages if p.get('targetId') in targets]
        for p in aliases:
            proof = registered.get(p.get('targetId'), {})
            if (p.get('groupId') != group_id or p.get('ownership') != 'user'
                    or p.get('ownerAgentId') is not None or type(p.get('tabId')) is not int
                    or p['tabId'] in owned_ids or p['tabId'] not in saved[0]['tabIds']
                    or not isinstance(p.get('url'), str) or credential_bearing_url(p['url'])
                    or proof.get('tabId') != p['tabId'] or proof.get('url') != p['url']):
                return False
        tab_ids = {p['tabId'] for p in members}
        return len(tab_ids) == len(targets) and tab_ids == set(current[0]['tabIds'])

    def restored_candidates(self, pages, groups, *, native_targets=None, observed_session=None):
        """Missing bindings after reconnection stay pending, even after renaming."""
        record = self.record or {}
        current = observed_session if observed_session is not None else record.get('session')
        if record.get('pages_session') == current:
            return []
        physical = {p.get('targetId') for p in pages}
        result = []
        for old_id in {p.get('groupId') for p in record.get('pages', [])}:
            old = [p for p in record['pages'] if p.get('groupId') == old_id]
            if self._registered_unclaimed_remainder(old_id, old, pages, groups, native_targets):
                continue
            if physical.intersection(p['targetId'] for p in old):
                continue
            candidates = []
            for group in groups:
                gid = group.get('id', group.get('groupId'))
                members = [p for p in pages if p.get('groupId') == gid]
                unique = {p['targetId']:p for p in members if p.get('targetId')}
                matches = Counter(p['url'] for p in unique.values()) == Counter(p['url'] for p in old)
                aliases = [p for p in pages if p.get('targetId') in unique]
                if unique and matches:
                    candidates.append(dict(groupId=gid,blockedByOwner=any(other_owner(p) for p in aliases),
                        pages=[dict(targetId=t,url=p['url']) for t,p in unique.items()]))
            # Neither a shared URL nor native ownership of a different target
            # acknowledges this prior group. Only an explicit verified recovery
            # may discharge it at save time.
            result.append(dict(priorGroupId=old_id,candidates=candidates))
        return result

    def restored_recovery(self, recovery, pages, groups):
        """Explicit human-selected restoration, never title/URL-based admission."""
        group_id, members = self.recovery_group(recovery, pages, groups)
        old = [p for p in self.record['pages'] if p.get('groupId') == recovery['prior_group_id']]
        targets = {p['targetId'] for p in old}
        if targets.intersection(p.get('targetId') for p in pages):
            raise ValueError('Original physical targets still exist; restored recovery is forbidden')
        expected = {p['targetId']: p['url'] for p in members}
        if (len({p['url'] for p in old}) != len(old)
                or len(set(expected.values())) != len(expected)
                or Counter(p['url'] for p in old) != Counter(expected.values())
                or any(type(p.get('tabId')) is not int or credential_bearing_url(p['url']) for p in old)):
            raise ValueError('Restored task needs complete unambiguous original tab/target/URL provenance')
        proof = {}
        owner_session = self.record.get('pages_session')
        if owner_session:
            proof[owner_session] = targets
        for prior in self.record.get('prior_bindings', []):
            if (prior.get('targetId') in targets and prior.get('groupId') == recovery['prior_group_id']
                    and any(p['targetId'] == prior['targetId'] and p['url'] == prior.get('url') for p in old)):
                proof.setdefault(prior['session'], set()).add(prior['targetId'])
        for key, _ in self.record.get('retained', []):
            if len(key) == 3 and any(p['targetId'] == key[1] and p['url'] == key[2] for p in old):
                proof.setdefault(key[0], set()).add(key[1])
        handles = [s for s, ts in proof.items() if ts == targets and s != self.record.get('session')]
        if len(handles) != 1:
            raise ValueError('Original task session proof is missing or ambiguous; preserve binding')
        return dict(groupId=group_id, priorGroupId=recovery['prior_group_id'], priorSession=handles[0],
            prior=[{k:p[k] for k in ('tabId','targetId','url')} for p in old],
            expected=[dict(targetId=t,url=u) for t,u in sorted(expected.items())])

    def expired_recovery(self, pages, groups):
        """Supply exact local task provenance, never infer server-side expiry.

        Every saved physical member must have a consistent foreign owner. The
        server still verifies the original handle's end and physical claims.
        """
        record = self.record or {}
        requests = []
        for group_id in {p.get('groupId') for p in record.get('pages', [])}:
            old = [p for p in record['pages'] if p.get('groupId') == group_id]
            expected = {p['targetId']:p['url'] for p in old}
            aliases = [p for p in pages if p.get('targetId') in expected]
            members = [p for p in pages if p.get('groupId') == group_id]
            if (not group_id or sum(g.get('id',g.get('groupId')) == group_id for g in groups) != 1
                    or {p.get('targetId'):p.get('url') for p in members} != expected
                    or {p.get('targetId') for p in aliases} != set(expected)
                    or any(p.get('groupId') != group_id or p.get('url') != expected[p['targetId']]
                           or p.get('ownership') == 'mine' for p in aliases)):
                continue
            owners = {p.get('ownerAgentId') for p in aliases if other_owner(p)}
            unclaimed = all(p.get('ownership') == 'user' and p.get('ownerAgentId') is None for p in aliases)
            if not unclaimed and (len(owners) != 1 or not all(isinstance(o,str) and o for o in owners)
                    or any(not any(other_owner(p) for p in aliases if p['targetId'] == target) for target in expected)):
                continue
            proof = {}
            for prior in record.get('prior_bindings', []):
                if expected.get(prior.get('targetId')) == prior.get('url') and prior.get('groupId') == group_id:
                    proof.setdefault(prior['session'], set()).add(prior['targetId'])
            for key, _ in record.get('retained', []):
                if len(key) == 3 and expected.get(key[1]) == key[2]:
                    proof.setdefault(key[0], set()).add(key[1])
            handles = [handle for handle, targets in proof.items()
                       if targets == set(expected) and handle != record.get('session')]
            if len(handles) == 1:
                requests.append(dict(groupId=group_id, priorSession=handles[0],
                    expected=[dict(targetId=p['targetId'],url=p['url']) for p in old]))
        return requests

    def reconciliation(self, pages, groups, *, recovery=None, allow_expired=False, native_targets=None):
        """Return unique proven groups or unresolved conflicts, without mutations."""
        selected = self.recovery_group(recovery, pages, groups) if recovery is not None else None
        if not self.record or not self.record['pages']:
            return [], []
        physical = {p['targetId']:p for p in pages if p.get('targetId')}
        if native_targets is not None and (not isinstance(native_targets,list)
                or any(not isinstance(t,str) or not t for t in native_targets)
                or not set(physical).issubset(native_targets)):
            raise ValueError('Incomplete native target inventory; preserve prior bindings')
        physical_targets = set(physical) if native_targets is None else set(native_targets)
        expired = {r['groupId'] for r in self.expired_recovery(pages, groups)} if allow_expired else set()
        candidates, unresolved = [], []
        for old_id in {p.get('groupId') for p in self.record['pages']}:
            old = [p for p in self.record['pages'] if p.get('groupId') == old_id]
            old_targets = {p['targetId'] for p in old}
            if old_targets.intersection(physical_targets).difference(physical):
                unresolved.append({'groupId':old_id,
                    'reason':'Recorded physical target is missing from SDK inventory; no URL-based claim'})
                continue
            if self._registered_unclaimed_remainder(old_id, old, pages, groups, native_targets):
                # Physical closure retires only the old mine binding. The
                # registered user tabs stay unclaimed and require cleanup review.
                continue
            if (not old_targets.intersection(physical_targets)
                    and self._has_preexisting_sibling(old_id, old, pages)):
                unresolved.append({'groupId':old_id,
                    'reason':'Previously unowned group member has incomplete registration; no URL-based claim'})
                continue
            if selected and old_id == recovery['prior_group_id']:
                owned={p['targetId'] for p in selected[1] if p.get('ownership')=='mine'}
                if not {p['targetId'] for p in selected[1]}.issubset(owned):
                    candidates.append(selected[0])
                continue
            if any(p.get('targetId') in old_targets and
                   other_owner(p) for p in pages):
                if old_id in expired:
                    candidates.append(old_id)
                    continue
                unresolved.append({'groupId':old_id,'reason':'Another active or unknown owner occupies a recorded physical target'})
                continue
            owned_targets={p['targetId'] for p in pages if p.get('ownership')=='mine' and p.get('targetId')}
            surviving=old_targets.intersection(physical_targets)
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
                # Session titles are shared by distinct groups. Native target
                # absence plus exact group absence proves closure even when an
                # unrelated group keeps that title; title/URL matches still go
                # through the existing recovery and foreign-owner checks.
                related = related or gid == old_id or (bool(same) and native_targets is None)
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
            elif hits or related or old_targets.intersection(physical_targets):
                unresolved.append({'groupId':old_id, 'reason':'Ownership/membership conflict; no automatic claim'})
            # Actual absence with no matching restored group means closed; a URL
            # elsewhere does not establish this task's ownership.
        return candidates, unresolved
