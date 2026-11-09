from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import time

from ._path_safety import (
    is_link_or_reparse as _is_link_or_reparse,
    lstat_or_none as _lstat_or_none,
    reject_raw_link_or_reparse_components,
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    max_files: int | None = None
    max_age_days: float | None = None
    max_total_bytes: int | None = None

    def validate(self) -> None:
        if self.max_files is not None and self.max_files < 0:
            raise ValueError("max_files cannot be negative")
        if self.max_age_days is not None and self.max_age_days < 0:
            raise ValueError("max_age_days cannot be negative")
        if self.max_total_bytes is not None and self.max_total_bytes < 0:
            raise ValueError("max_total_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    directory: Path
    execute: bool
    kept: tuple[Path, ...]
    removed: tuple[Path, ...]
    reclaimed_bytes: int
    created_utc: str
    deleted: tuple[Path, ...] = ()
    failures: tuple["RetentionFailure", ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionFailure:
    path: Path
    error: str


def prune_files(
    directory: str | os.PathLike[str],
    policy: RetentionPolicy,
    *,
    pattern: str = "*",
    recursive: bool = False,
    execute: bool = False,
    now: float | None = None,
) -> RetentionPlan:
    """Plan retention, deleting regular files only when ``execute=True``.

    The default dry-run is deliberate.  Directories, symbolic links, reparse
    points, and internal writer lock files are never removed.  Before unlink,
    each selected regular file is revalidated against its planned identity;
    changes and deletion errors are returned in ``RetentionPlan.failures``.
    """

    policy.validate()
    _validate_pattern(pattern)
    root_input = Path(directory).expanduser()
    if not root_input.is_absolute():
        raise ValueError(f"retention root must be absolute: {root_input}")
    reject_raw_link_or_reparse_components(root_input, label="retention root")
    root_metadata = _lstat_or_none(root_input)
    if root_metadata is None:
        return RetentionPlan(
            directory=root_input.resolve(strict=False),
            execute=execute,
            kept=(),
            removed=(),
            reclaimed_bytes=0,
            created_utc=datetime.now(timezone.utc).isoformat(),
        )
    if _is_link_or_reparse(root_metadata):
        raise ValueError(
            f"retention root cannot be a link or reparse point: {root_input}"
        )
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"retention root is not a directory: {root_input}")

    root = root_input.resolve(strict=True)
    root_identity = _directory_identity(root)
    files = _collect_candidates(root, pattern, recursive=recursive)
    files.sort(key=lambda candidate: candidate.identity.mtime_ns, reverse=True)
    current_time = time.time() if now is None else now
    removed: set[Path] = set()
    identities = {candidate.path: candidate.identity for candidate in files}

    if policy.max_age_days is not None:
        cutoff = current_time - policy.max_age_days * 86400.0
        removed.update(
            candidate.path
            for candidate in files
            if candidate.identity.mtime_ns / 1_000_000_000 < cutoff
        )

    remaining = [candidate for candidate in files if candidate.path not in removed]
    if policy.max_files is not None:
        removed.update(candidate.path for candidate in remaining[policy.max_files :])
        remaining = remaining[: policy.max_files]

    if policy.max_total_bytes is not None:
        total = 0
        for candidate in remaining:
            size = candidate.identity.size
            if total + size > policy.max_total_bytes:
                removed.add(candidate.path)
            else:
                total += size

    ordered_removed = tuple(
        candidate.path for candidate in reversed(files) if candidate.path in removed
    )
    planned_reclaimed = sum(identities[path].size for path in ordered_removed)
    deleted: list[Path] = []
    failures: list[RetentionFailure] = []
    reclaimed = planned_reclaimed
    if execute:
        reclaimed = 0
        for path in ordered_removed:
            try:
                _revalidate_retention_root(root_input, root, root_identity)
            except (OSError, ValueError) as exc:
                failures.append(
                    RetentionFailure(path, f"retention root changed: {exc}")
                )
                continue
            if _path_has_link_or_reparse(root, path):
                failures.append(
                    RetentionFailure(
                        path, "path gained a link or reparse-point component"
                    )
                )
                continue
            try:
                current_identity = _file_identity(path)
            except OSError as exc:
                failures.append(
                    RetentionFailure(path, f"could not revalidate file: {exc}")
                )
                continue
            if current_identity != identities[path]:
                failures.append(
                    RetentionFailure(path, "file changed since retention planning")
                )
                continue
            try:
                path.unlink()
            except OSError as exc:
                failures.append(RetentionFailure(path, f"could not delete file: {exc}"))
                continue
            deleted.append(path)
            reclaimed += identities[path].size
    kept = tuple(candidate.path for candidate in files if candidate.path not in removed)
    return RetentionPlan(
        directory=root,
        execute=execute,
        kept=kept,
        removed=ordered_removed,
        reclaimed_bytes=reclaimed,
        created_utc=datetime.now(timezone.utc).isoformat(),
        deleted=tuple(deleted),
        failures=tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    path: Path
    identity: _FileIdentity


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    mode: int


def _collect_candidates(
    root: Path, pattern: str, *, recursive: bool
) -> list[_Candidate]:
    paths: list[Path] = []
    if recursive:
        for current_root, directory_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=_raise_walk_error,
        ):
            current = Path(current_root)
            directory_names[:] = [
                name
                for name in directory_names
                if not _path_is_link_or_reparse(current / name)
            ]
            for name in file_names:
                path = current / name
                relative = path.relative_to(root)
                if _matches_pattern(relative, pattern):
                    paths.append(path)
    else:
        # ``Path.glob()`` traverses when the caller supplies ``**`` or a path
        # separator, even though this API promises that recursive=False only
        # considers direct children.  Enumerate direct entries first so a
        # broad pattern can never silently widen a destructive operation.
        paths.extend(
            path
            for path in root.iterdir()
            if _matches_pattern(Path(path.name), pattern)
        )

    candidates: list[_Candidate] = []
    for path in paths:
        if _is_internal_lock_name(path.name) or _path_has_link_or_reparse(root, path):
            continue
        identity = _file_identity(path)
        if identity is not None:
            candidates.append(_Candidate(path, identity))
    return candidates


def _validate_pattern(pattern: str) -> None:
    if not pattern:
        raise ValueError("retention pattern cannot be empty")
    variants = (Path(pattern), PurePosixPath(pattern), PureWindowsPath(pattern))
    if any(path.is_absolute() or path.anchor or path.drive for path in variants):
        raise ValueError(f"retention pattern must be relative: {pattern}")
    if any(part == ".." for path in variants for part in path.parts):
        raise ValueError(f"retention pattern cannot escape its root: {pattern}")


def _matches_pattern(relative: Path, pattern: str) -> bool:
    if relative.match(pattern):
        return True
    return pattern.startswith("**/") and relative.match(pattern[3:])


def _file_identity(path: Path) -> _FileIdentity | None:
    metadata = path.lstat()
    if _is_link_or_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        return None
    return _FileIdentity(
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


def _revalidate_retention_root(
    root_input: Path,
    root: Path,
    expected_identity: _DirectoryIdentity,
) -> None:
    reject_raw_link_or_reparse_components(root_input, label="retention root")
    if root_input.resolve(strict=True) != root:
        raise ValueError("raw root now resolves to a different directory")
    if _directory_identity(root) != expected_identity:
        raise ValueError("root directory identity changed")


def _path_has_link_or_reparse(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    root_metadata = _lstat_or_none(root)
    if root_metadata is None or _is_link_or_reparse(root_metadata):
        return True
    current = root
    for part in relative.parts:
        current /= part
        metadata = _lstat_or_none(current)
        if metadata is not None and _is_link_or_reparse(metadata):
            return True
    return False


def _path_is_link_or_reparse(path: Path) -> bool:
    metadata = _lstat_or_none(path)
    return metadata is None or _is_link_or_reparse(metadata)


def _is_internal_lock_name(name: str) -> bool:
    return name.startswith(".") and name.endswith(".lock") and bool(name[1:-5])


def _raise_walk_error(error: OSError) -> None:
    raise error
