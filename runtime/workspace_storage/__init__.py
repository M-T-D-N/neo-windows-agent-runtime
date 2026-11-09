"""Shared storage paths and safe file operations.

The public API is intentionally small.  Projects own their schemas and
migrations; this package owns only filesystem placement and write safety.
"""

from .atomic import (
    RecoveryResult,
    StorageReadError,
    StorageVersionError,
    StorageWriteError,
    atomic_copy_file,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    load_json_with_backups,
    load_with_backups,
)
from ._path_safety import ensure_no_reparse_components
from .migration import MigrationEntry, MigrationReport, copy_legacy_tree
from .paths import AppStorage, StoragePathError, resolve_storage
from .retention import RetentionFailure, RetentionPlan, RetentionPolicy, prune_files

__all__ = [
    "AppStorage",
    "MigrationEntry",
    "MigrationReport",
    "RecoveryResult",
    "RetentionFailure",
    "RetentionPlan",
    "RetentionPolicy",
    "StoragePathError",
    "StorageReadError",
    "StorageVersionError",
    "StorageWriteError",
    "atomic_copy_file",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "copy_legacy_tree",
    "ensure_no_reparse_components",
    "load_json_with_backups",
    "load_with_backups",
    "prune_files",
    "resolve_storage",
]
