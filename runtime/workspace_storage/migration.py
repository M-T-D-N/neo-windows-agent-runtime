from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import stat
import uuid

from ._path_safety import (
    is_link_or_reparse as _is_link_or_reparse,
    lstat_or_none as _lstat_or_none,
    reject_raw_link_or_reparse_components,
)
from .atomic import StorageWriteError, _file_lock, atomic_copy_file


@dataclass(frozen=True, slots=True)
class MigrationEntry:
    source: Path
    destination: Path
    size: int
    status: str
    sha256: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationReport:
    source: Path
    destination: Path
    execute: bool
    entries: tuple[MigrationEntry, ...]

    @property
    def copied(self) -> int:
        return sum(entry.status == "copied" for entry in self.entries)

    @property
    def conflicts(self) -> int:
        return sum(entry.status == "conflict" for entry in self.entries)

    @property
    def skipped(self) -> int:
        return sum(entry.status == "skipped" for entry in self.entries)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": str(self.source),
            "destination": str(self.destination),
            "execute": self.execute,
            "copied": self.copied,
            "conflicts": self.conflicts,
            "skipped": self.skipped,
            "entries": [
                {
                    **asdict(entry),
                    "source": str(entry.source),
                    "destination": str(entry.destination),
                }
                for entry in self.entries
            ],
        }


def copy_legacy_tree(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    execute: bool = False,
    max_files: int = 100_000,
    max_bytes: int = 20 * 1024 * 1024 * 1024,
) -> MigrationReport:
    """Plan or execute an idempotent, copy-only legacy migration.

    Existing different files are conflicts and are never overwritten.  Links,
    reparse points, and exact internal lock/temporary files are reported as
    skipped.  Source files are never modified or deleted, and source and
    destination trees may not overlap.
    """

    origin_input = Path(source).expanduser()
    target_input = Path(destination).expanduser()
    if not origin_input.is_absolute():
        raise ValueError(f"legacy source must be absolute: {origin_input}")
    if not target_input.is_absolute():
        raise ValueError(f"migration destination must be absolute: {target_input}")
    reject_raw_link_or_reparse_components(
        origin_input,
        label="legacy source",
    )
    reject_raw_link_or_reparse_components(
        target_input,
        label="migration destination",
    )
    origin_stat = _lstat_or_none(origin_input)
    if origin_stat is None or not stat.S_ISDIR(origin_stat.st_mode):
        raise ValueError(f"legacy source is not a directory: {origin_input}")
    if _is_link_or_reparse(origin_stat):
        raise ValueError(
            f"legacy source cannot be a link or reparse point: {origin_input}"
        )

    target_stat = _lstat_or_none(target_input)
    if target_stat is not None:
        if _is_link_or_reparse(target_stat):
            raise ValueError(
                f"migration destination cannot be a link or reparse point: {target_input}"
            )
        if not stat.S_ISDIR(target_stat.st_mode):
            raise ValueError(
                f"migration destination is not a directory: {target_input}"
            )

    origin = origin_input.resolve(strict=True)
    target_root = target_input.resolve(strict=False)
    if _paths_overlap(origin, target_root):
        raise ValueError(
            f"legacy source and migration destination cannot overlap: {origin} <-> {target_root}"
        )

    scanned = _scan_source_tree(origin)
    copy_candidates = [entry for entry in scanned if entry.reason is None]
    if len(copy_candidates) > max_files:
        raise ValueError("legacy migration exceeds the file-count limit")
    total_bytes = sum(entry.size for entry in copy_candidates)
    if total_bytes > max_bytes:
        raise ValueError("legacy migration exceeds the byte limit")

    if execute:
        target_root.mkdir(parents=True, exist_ok=True)
        source_root_identity = _directory_identity(origin)
        target_root_identity = _directory_identity(target_root)
        _revalidate_roots(
            origin_input,
            target_input,
            origin,
            target_root,
            source_root_identity,
            target_root_identity,
        )
        with _file_lock(target_root / "workspace-storage-migration", timeout=5.0):
            # A destination lock only coordinates callers that honor it.  Raw
            # ancestors and both root identities are therefore checked again
            # after acquisition before any destination file can be installed.
            _revalidate_roots(
                origin_input,
                target_input,
                origin,
                target_root,
                source_root_identity,
                target_root_identity,
            )
            entries = _build_migration_entries(
                scanned,
                target_root,
                execute=True,
            )
    else:
        entries = _build_migration_entries(
            scanned,
            target_root,
            execute=False,
        )
    return MigrationReport(origin, target_root, execute, tuple(entries))


def _build_migration_entries(
    scanned: list[_ScannedEntry],
    target_root: Path,
    *,
    execute: bool,
) -> list[MigrationEntry]:
    entries: list[MigrationEntry] = []
    for scanned_entry in scanned:
        path = scanned_entry.path
        relative = scanned_entry.relative
        destination_path = target_root / relative
        size = scanned_entry.size
        if scanned_entry.reason is not None:
            entries.append(
                MigrationEntry(
                    source=path,
                    destination=destination_path,
                    size=size,
                    status="skipped",
                    reason=scanned_entry.reason,
                )
            )
            continue

        destination_issue = _destination_reparse_issue(target_root, relative)
        if destination_issue is not None:
            entries.append(
                MigrationEntry(
                    source=path,
                    destination=destination_path,
                    size=size,
                    status="conflict",
                    reason=destination_issue,
                )
            )
            continue

        source_hash, source_identity = _stable_sha256(path)
        destination_stat = _lstat_or_none(destination_path)
        if destination_stat is not None and stat.S_ISREG(destination_stat.st_mode):
            status = (
                "already-present"
                if _sha256(destination_path) == source_hash
                else "conflict"
            )
            entries.append(
                MigrationEntry(
                    source=path,
                    destination=destination_path,
                    size=size,
                    status=status,
                    sha256=source_hash,
                    reason=None if status == "already-present" else "different-content",
                )
            )
            continue
        if destination_stat is not None:
            entries.append(
                MigrationEntry(
                    source=path,
                    destination=destination_path,
                    size=size,
                    status="conflict",
                    sha256=source_hash,
                    reason="destination-not-regular-file",
                )
            )
            continue
        status = "planned"
        if execute:
            # The whole destination tree is protected by the single migration
            # lock above, so immutable copied files do not each leave a
            # permanent sidecar lock behind.
            try:
                atomic_copy_file(path, destination_path, use_lock=False)
            except StorageWriteError as exc:
                # A process that does not honor the migration lock may win the
                # create-if-absent race.  Classify its file, never replace it.
                if exc.final_installed:
                    raise
                destination_stat = _lstat_or_none(destination_path)
                if destination_stat is None:
                    raise
                if stat.S_ISREG(destination_stat.st_mode) and not _is_link_or_reparse(
                    destination_stat
                ):
                    destination_hash = _sha256(destination_path)
                    status = (
                        "already-present"
                        if destination_hash == source_hash
                        else "conflict"
                    )
                    entries.append(
                        MigrationEntry(
                            source=path,
                            destination=destination_path,
                            size=size,
                            status=status,
                            sha256=source_hash,
                            reason=(
                                None
                                if status == "already-present"
                                else "destination-created-during-migration"
                            ),
                        )
                    )
                    continue
                entries.append(
                    MigrationEntry(
                        source=path,
                        destination=destination_path,
                        size=size,
                        status="conflict",
                        sha256=source_hash,
                        reason="destination-created-during-migration",
                    )
                )
                continue

            created_identity = _regular_file_identity(destination_path)
            try:
                destination_hash, verified_destination_identity = _stable_sha256(
                    destination_path
                )
                current_source_identity = _regular_file_identity(path)
                if current_source_identity != source_identity:
                    raise OSError(f"migration source changed while copying: {path}")
                if verified_destination_identity != created_identity:
                    raise OSError(
                        f"migration destination changed during verification: "
                        f"{destination_path}"
                    )
                if destination_hash != source_hash:
                    raise OSError(
                        f"migration verification failed for {destination_path}"
                    )
            except Exception as exc:
                cleanup = _discard_unverified_destination(
                    destination_path,
                    created_identity,
                )
                raise OSError(f"{exc}; unverified destination {cleanup}") from exc
            status = "copied"
        entries.append(
            MigrationEntry(
                source=path,
                destination=destination_path,
                size=size,
                status=status,
                sha256=source_hash,
            )
        )
    return entries


@dataclass(frozen=True, slots=True)
class _ScannedEntry:
    path: Path
    relative: Path
    size: int
    reason: str | None = None


def _scan_source_tree(root: Path) -> list[_ScannedEntry]:
    scanned: list[_ScannedEntry] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            children = sorted(iterator, key=lambda entry: entry.name.casefold())
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root)
            metadata = child.stat(follow_symlinks=False)
            if _is_link_or_reparse(metadata):
                scanned.append(
                    _ScannedEntry(
                        path, relative, metadata.st_size, "link-or-reparse-point"
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                scanned.append(
                    _ScannedEntry(
                        path,
                        relative,
                        metadata.st_size,
                        _internal_transient_reason(path.name),
                    )
                )
            else:
                scanned.append(
                    _ScannedEntry(path, relative, metadata.st_size, "non-regular-file")
                )
    scanned.sort(key=lambda entry: str(entry.relative).casefold())
    return scanned


def _internal_transient_reason(name: str) -> str | None:
    if name.startswith(".") and name.endswith(".lock") and name[1:-5]:
        return "internal-lock"
    if name.startswith(".") and name.endswith(".tmp"):
        stem, separator, token = name[1:-4].rpartition(".")
        if (
            separator
            and stem
            and len(token) == 8
            and all(
                character.isascii() and (character.isalnum() or character == "_")
                for character in token
            )
        ):
            return "internal-temp"
    return None


def _destination_reparse_issue(root: Path, relative: Path) -> str | None:
    current = root
    for index, part in enumerate(relative.parts):
        current /= part
        metadata = _lstat_or_none(current)
        if metadata is None:
            return None
        if _is_link_or_reparse(metadata):
            return "destination-link-or-reparse-point"
        if index < len(relative.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return "destination-parent-not-directory"
    return None


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _NodeIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int


def _regular_file_identity(path: Path) -> _NodeIdentity:
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"path is no longer a regular file: {path}")
    return _NodeIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _directory_identity(path: Path) -> _DirectoryIdentity:
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"path is no longer a regular directory: {path}")
    return _DirectoryIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
    )


def _stable_sha256(path: Path) -> tuple[str, _NodeIdentity]:
    before = _regular_file_identity(path)
    digest = _sha256(path)
    after = _regular_file_identity(path)
    if after != before:
        raise OSError(f"file changed while hashing: {path}")
    return digest, after


def _revalidate_roots(
    origin_input: Path,
    target_input: Path,
    origin: Path,
    target_root: Path,
    source_identity: _DirectoryIdentity,
    target_identity: _DirectoryIdentity,
) -> None:
    reject_raw_link_or_reparse_components(origin_input, label="legacy source")
    reject_raw_link_or_reparse_components(
        target_input,
        label="migration destination",
    )
    if origin_input.resolve(strict=True) != origin:
        raise ValueError("legacy source changed during migration setup")
    if target_input.resolve(strict=True) != target_root:
        raise ValueError("migration destination changed during migration setup")
    if _directory_identity(origin) != source_identity:
        raise ValueError("legacy source identity changed during migration setup")
    if _directory_identity(target_root) != target_identity:
        raise ValueError("migration destination identity changed during setup")


def _discard_unverified_destination(
    destination: Path,
    expected_identity: _NodeIdentity,
) -> str:
    """Remove, or quarantine, only the exact file installed by this call."""

    try:
        current_identity = _regular_file_identity(destination)
    except FileNotFoundError:
        return "was already absent"
    except OSError as exc:
        return f"was not removed because it could not be identified: {exc}"
    if current_identity != expected_identity:
        return "was not removed because its identity changed"

    try:
        destination.unlink()
        return "was removed"
    except OSError as unlink_error:
        # A unique quarantine name gets the unverified file out of the final
        # namespace when deletion is denied but rename remains possible.
        quarantine = destination.with_name(
            f".{destination.name}.unverified-{uuid.uuid4().hex}.tmp"
        )
        try:
            if _regular_file_identity(destination) != expected_identity:
                return "was not quarantined because its identity changed"
            os.replace(destination, quarantine)
        except OSError as quarantine_error:
            return (
                "could not be removed or quarantined "
                f"({unlink_error}; {quarantine_error})"
            )
        return f"was quarantined at {quarantine}"
