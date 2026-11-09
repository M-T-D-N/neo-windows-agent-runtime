"""Fail-closed publishing intent ledger; it does not submit to any website."""
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from workspace_storage import atomic_write_json, StorageVersionError, StorageWriteError
from .storage import FileLock

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")
STATES = {"PREPARED", "SUBMITTING", "NEEDS_RECONCILIATION", "VERIFIED", "FAILED_CONFIRMED"}
PENDING = {"SUBMITTING", "NEEDS_RECONCILIATION"}
IDENTITY = ("domain", "account", "channel", "manuscript_version", "attachments", "approval_reference")


class LedgerError(ValueError):
    pass


class Blocked(LedgerError):
    def __init__(self, reason, operation=None):
        super().__init__(reason)
        self.operation = copy.deepcopy(operation)


def text(value, name, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise LedgerError(f"invalid {name}")
    return value


def slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise LedgerError("operation_id must be a lowercase slug of at most 80 characters")
    return value


def identity(values):
    if set(values) != set(IDENTITY):
        raise LedgerError("identity fields must exactly match the documented contract")
    domain = text(values["domain"], "domain", 253)
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", domain):
        raise LedgerError("domain must be a lowercase hostname without URL, port or path")
    for name in IDENTITY:
        if name not in {"domain", "attachments"}:
            text(values[name], name)
    attachments = values["attachments"]
    if not isinstance(attachments, list) or len(attachments) > 32:
        raise LedgerError("attachments must be an ordered list of at most 32 identifiers")
    for item in attachments:
        text(item, "attachment identifier", 256)
    if len(set(attachments)) != len(attachments):
        raise LedgerError("attachment identifiers must be distinct")
    return copy.deepcopy(values)


def evidence(value, kind):
    if not isinstance(value, dict) or set(value) != {"kind", "reference"} or value["kind"] != kind:
        raise LedgerError(f"evidence requires kind={kind} and reference")
    text(value["reference"], "evidence reference", 1024)


def result(value, domain):
    if not isinstance(value, dict) or set(value) != {"post_id", "url", "evidence"}:
        raise LedgerError("verified result requires post_id, url and evidence")
    text(value["post_id"], "post ID", 256)
    url = urlsplit(text(value["url"], "post URL", 2048))
    if url.scheme not in {"http", "https"} or url.hostname != domain or url.username or url.password or not url.path:
        raise LedgerError("post URL must identify a post on the approved domain")
    if domain not in {"localhost", "127.0.0.1"} and url.scheme != "https":
        raise LedgerError("non-loopback post URLs require https")
    evidence(value["evidence"], "post-readback")


def validate(state):
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int:
        raise LedgerError("ledger requires an integer schema_version")
    if state["schema_version"] > 1:
        raise StorageVersionError("future ledger schema; preserve file and use a compatible version")
    if state["schema_version"] != 1 or set(state) != {"schema_version", "operations"} or not isinstance(state["operations"], dict):
        raise LedgerError("unsupported or malformed ledger")
    busy = set()
    for op_id, record in state["operations"].items():
        slug(op_id)
        if not isinstance(record, dict) or set(record) != {"operation_id", "identity", "status", "updated_at", "result", "evidence"}:
            raise LedgerError("malformed operation")
        if record["operation_id"] != op_id or not isinstance(record["status"], str) or record["status"] not in STATES:
            raise LedgerError("invalid operation identity or status")
        ident = identity(record["identity"])
        text(record["updated_at"], "updated_at", 64)
        if record["status"] == "VERIFIED":
            result(record["result"], ident["domain"])
            if record["evidence"] != record["result"]["evidence"]:
                raise LedgerError("verified evidence mismatch")
        elif record["result"] is not None:
            raise LedgerError("unverified operation cannot have a result")
        if record["status"] == "FAILED_CONFIRMED":
            evidence(record["evidence"], "publisher-confirmed-absence")
        elif record["status"] == "NEEDS_RECONCILIATION":
            if record["evidence"] is not None:
                evidence(record["evidence"], "uncertainty")
        elif record["status"] != "VERIFIED" and record["evidence"] is not None:
            raise LedgerError("unexpected evidence")
        if record["status"] in PENDING:
            account = (ident["domain"], ident["account"])
            if account in busy:
                raise LedgerError("ledger contains conflicting pending operations")
            busy.add(account)
    return state


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            raise LedgerError("ledger path must be absolute")
        self.transaction = self.path.with_name(self.path.name + ".transaction")

    def _read(self):
        try:
            with self.path.open(encoding="utf-8-sig") as stream:
                state = json.load(stream)
        except FileNotFoundError:
            if any(self.path.with_name(self.path.name + f".bak.{i}").exists() for i in range(1, 4)):
                raise LedgerError("missing ledger with existing backups; inspect before explicit recovery")
            state = {"schema_version": 1, "operations": {}}
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise LedgerError("unreadable ledger; preserve original and inspect backups, no automatic restoration") from exc
        return validate(state)

    def _write(self, state):
        validate(state)
        try:
            atomic_write_json(self.path, state, require_schema_version=True, backup_count=3)
        except StorageWriteError as exc:
            if exc.final_installed:
                raise LedgerError(f"final_installed=True: stop submission; inspect installed ledger {self.path} and recovery_paths={exc.recovery_paths}; do not blindly retry") from exc
            raise

    @staticmethod
    def _update(record, status):
        record["status"] = status
        record["updated_at"] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _get(state, op_id):
        slug(op_id)
        try:
            return state["operations"][op_id]
        except KeyError as exc:
            raise LedgerError("unknown operation_id") from exc

    def status(self, op_id=None):
        with FileLock(self.transaction):
            state = self._read()
            return copy.deepcopy(self._get(state, op_id) if op_id else state)

    def prepare(self, op_id, **values):
        slug(op_id)
        ident = identity(values)
        with FileLock(self.transaction):
            state = self._read()
            existing = state["operations"].get(op_id)
            if existing:
                if existing["identity"] != ident:
                    raise Blocked("operation identity is immutable; intentional repost requires new approval and operation_id", existing)
                return copy.deepcopy(existing)
            record = {"operation_id": op_id, "identity": ident, "status": "PREPARED", "updated_at": "", "result": None, "evidence": None}
            self._update(record, "PREPARED")
            state["operations"][op_id] = record
            self._write(state)
            return copy.deepcopy(record)

    def begin(self, op_id):
        with FileLock(self.transaction):
            state = self._read()
            record = self._get(state, op_id)
            if record["status"] == "VERIFIED":
                return copy.deepcopy(record)
            if record["status"] in PENDING:
                self._update(record, "NEEDS_RECONCILIATION")
                self._write(state)
                raise Blocked("previous submit may have committed; reconcile, never resubmit", record)
            if record["status"] != "PREPARED":
                raise Blocked("terminal operation cannot be retried; new approval and operation_id required", record)
            for other in state["operations"].values():
                if other["status"] in PENDING and all(other["identity"][key] == record["identity"][key] for key in ("domain", "account")):
                    raise Blocked(f"account is busy with {other['operation_id']}; reconcile it first", record)
            self._update(record, "SUBMITTING")
            self._write(state)
            return copy.deepcopy(record)

    def uncertain(self, op_id, reference):
        with FileLock(self.transaction):
            state = self._read()
            record = self._get(state, op_id)
            if record["status"] not in PENDING:
                raise Blocked("only pending submissions can become uncertain", record)
            value = {"kind": "uncertainty", "reference": reference}
            evidence(value, "uncertainty")
            record["evidence"] = value
            self._update(record, "NEEDS_RECONCILIATION")
            self._write(state)
            return copy.deepcopy(record)

    def reconcile(self, op_id, status, *, verification, post_id=None, url=None):
        if status not in {"VERIFIED", "FAILED_CONFIRMED"}:
            raise LedgerError("reconciliation must confirm a post or positive absence")
        with FileLock(self.transaction):
            state = self._read()
            record = self._get(state, op_id)
            if record["status"] == "VERIFIED":
                return copy.deepcopy(record)
            if record["status"] not in PENDING:
                raise Blocked("only a pending submission can be reconciled", record)
            if status == "VERIFIED":
                value = {"post_id": post_id, "url": url, "evidence": verification}
                result(value, record["identity"]["domain"])
                record["result"] = value
            else:
                if post_id is not None or url is not None:
                    raise LedgerError("absence reconciliation cannot include a post result")
                evidence(verification, "publisher-confirmed-absence")
            record["evidence"] = copy.deepcopy(verification)
            self._update(record, status)
            self._write(state)
            return copy.deepcopy(record)
