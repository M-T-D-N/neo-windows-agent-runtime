"""Small project adapter for workspace_storage's installed locking API."""
from pathlib import Path
from workspace_storage.atomic import _file_lock


def FileLock(path: Path, timeout: float = 5.0):
    # workspace-storage 0.1.0 has no public FileLock. Keep the private API here.
    # Transaction lock is distinct from the final-file lock in atomic_write_json.
    return _file_lock(path, timeout=timeout)
