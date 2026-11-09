from __future__ import annotations

import os
from pathlib import Path
import stat


def reject_raw_link_or_reparse_components(
    path: str | os.PathLike[str],
    *,
    label: str,
) -> None:
    ensure_no_reparse_components(path, label=label)


def ensure_no_reparse_components(
    path: str | os.PathLike[str],
    *,
    label: str = "storage path",
) -> Path:
    """Reject links/reparse points in the caller-supplied path before resolve().

    ``Path.resolve()`` intentionally hides the fact that a raw path traversed a
    symbolic link or Windows junction.  Destructive/copying operations use this
    lexical walk first so an otherwise valid resolved path cannot widen their
    filesystem boundary.
    """

    raw = Path(path).expanduser()
    if raw.drive and not raw.is_absolute():
        raise ValueError(f"{label} cannot use a drive-relative path: {raw}")

    if raw.is_absolute():
        current = Path(raw.anchor)
        parts = raw.parts[1:]
        _reject_if_link_or_reparse(current, label=label)
    else:
        current = Path.cwd()
        # A relative raw path also inherits every existing component of cwd.
        for ancestor in reversed((current, *current.parents)):
            _reject_if_link_or_reparse(ancestor, label=label)
        parts = raw.parts

    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        current /= part
        _reject_if_link_or_reparse(current, label=label)
    return raw


def is_link_or_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(file_attributes & reparse_flag)


def lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _reject_if_link_or_reparse(path: Path, *, label: str) -> None:
    metadata = lstat_or_none(path)
    if metadata is not None and is_link_or_reparse(metadata):
        raise ValueError(f"{label} cannot traverse a link or reparse point: {path}")
