from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")


class StorageWriteError(OSError):
    """A storage operation failed, with installation state made explicit."""

    def __init__(
        self,
        message: str,
        *,
        final_installed: bool = False,
        recovery_paths: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(message)
        self.final_installed = final_installed
        self.recovery_paths = recovery_paths


class StorageReadError(ValueError):
    """Neither a primary file nor any retained backup could be decoded."""


class StorageVersionError(StorageReadError):
    """A readable document uses a newer schema that must not be downgraded."""


class _CopyCheckpointRaised(Exception):
    """Internal wrapper that guarantees temp cleanup before callback propagation."""

    def __init__(self, error: BaseException) -> None:
        super().__init__("copy checkpoint raised")
        self.error = error


@dataclass(frozen=True, slots=True)
class RecoveryResult(Generic[T]):
    value: T
    source: Path
    recovered: bool
    restored_primary: bool = False
    preserved_corrupt: Path | None = None


def atomic_write_bytes(
    path: str | os.PathLike[str],
    data: bytes,
    *,
    backup_count: int = 0,
    lock_timeout: float = 5.0,
    use_lock: bool = True,
) -> Path:
    destination = Path(path)
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")
    if not use_lock and backup_count:
        raise ValueError("use_lock=False requires backup_count=0")
    temporary: Path | None = None
    final_installed = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        lock_context = (
            _file_lock(destination, timeout=lock_timeout) if use_lock else nullcontext()
        )
        with lock_context:
            temporary = _temporary_path(destination)
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _install_temporary(
                temporary,
                destination,
                backup_count=backup_count,
                require_absent=not use_lock,
            )
            final_installed = True
            temporary = None
    except Exception as exc:
        preserve_temporary = (
            temporary is not None
            and isinstance(exc, StorageWriteError)
            and exc.final_installed
            and temporary in exc.recovery_paths
        )
        if temporary is not None and not preserve_temporary:
            _unlink_if_present(temporary)
        if isinstance(exc, StorageWriteError):
            raise
        state = " after installing the final file" if final_installed else ""
        raise StorageWriteError(
            f"could not safely write {destination}{state}: {exc}",
            final_installed=final_installed,
        ) from exc
    return destination


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    backup_count: int = 0,
    lock_timeout: float = 5.0,
    use_lock: bool = True,
) -> Path:
    return atomic_write_bytes(
        path,
        text.encode(encoding),
        backup_count=backup_count,
        lock_timeout=lock_timeout,
        use_lock=use_lock,
    )


def atomic_write_json(
    path: str | os.PathLike[str],
    payload: object,
    *,
    backup_count: int = 0,
    require_schema_version: bool = False,
    indent: int | None = 2,
    lock_timeout: float = 5.0,
    use_lock: bool = True,
) -> Path:
    if require_schema_version and (
        not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
    ):
        raise ValueError("versioned JSON must contain an integer schema_version")
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
    )
    return atomic_write_text(
        path,
        rendered + "\n",
        backup_count=backup_count,
        lock_timeout=lock_timeout,
        use_lock=use_lock,
    )


def atomic_copy_file(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    backup_count: int = 0,
    lock_timeout: float = 5.0,
    use_lock: bool = True,
    checkpoint: Callable[[int], None] | None = None,
) -> Path:
    origin = Path(source)
    target = Path(destination)
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")
    if not use_lock and backup_count:
        raise ValueError("use_lock=False requires backup_count=0")
    if not origin.is_file() or origin.is_symlink():
        raise StorageWriteError(f"copy source is not a regular file: {origin}")
    temporary: Path | None = None
    final_installed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_context = (
            _file_lock(target, timeout=lock_timeout) if use_lock else nullcontext()
        )
        with lock_context:
            temporary = _temporary_path(target)
            with (
                origin.open("rb") as input_stream,
                temporary.open("wb") as output_stream,
            ):
                copied_bytes = 0
                while block := input_stream.read(1024 * 1024):
                    output_stream.write(block)
                    copied_bytes += len(block)
                    if checkpoint is not None:
                        try:
                            checkpoint(copied_bytes)
                        except BaseException as exc:
                            raise _CopyCheckpointRaised(exc) from exc
                output_stream.flush()
                os.fsync(output_stream.fileno())
            shutil.copystat(origin, temporary, follow_symlinks=False)
            _install_temporary(
                temporary,
                target,
                backup_count=backup_count,
                require_absent=not use_lock,
            )
            final_installed = True
            temporary = None
    except Exception as exc:
        preserve_temporary = (
            temporary is not None
            and isinstance(exc, StorageWriteError)
            and exc.final_installed
            and temporary in exc.recovery_paths
        )
        if temporary is not None and not preserve_temporary:
            _unlink_if_present(temporary)
        if isinstance(exc, _CopyCheckpointRaised):
            raise exc.error
        if isinstance(exc, StorageWriteError):
            raise
        state = " after installing the final file" if final_installed else ""
        raise StorageWriteError(
            f"could not safely copy {origin} to {target}{state}: {exc}",
            final_installed=final_installed,
        ) from exc
    return target


def load_with_backups(
    path: str | os.PathLike[str],
    parser: Callable[[bytes], T],
    *,
    backup_count: int = 3,
    restore: bool = False,
    lock_timeout: float = 5.0,
) -> RecoveryResult[T]:
    source = Path(path)
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")
    if restore and source.parent.is_dir():
        with _file_lock(source, timeout=lock_timeout):
            return _load_with_backups_locked(
                source,
                parser,
                backup_count=backup_count,
                restore=True,
            )
    return _load_with_backups_locked(
        source,
        parser,
        backup_count=backup_count,
        restore=False,
    )


def _load_with_backups_locked(
    source: Path,
    parser: Callable[[bytes], T],
    *,
    backup_count: int,
    restore: bool,
) -> RecoveryResult[T]:
    candidates = [
        source,
        *(_backup_path(source, index) for index in range(1, backup_count + 1)),
    ]
    errors: list[str] = []
    primary_raw: bytes | None = None
    for candidate in candidates:
        try:
            raw = candidate.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            # A permission, sharing, device, or other I/O failure does not
            # prove that the candidate is corrupt.  In particular, never
            # restore an older backup over a primary that was merely
            # unreadable for a moment.
            raise
        if candidate == source:
            primary_raw = raw
        try:
            value = parser(raw)
        except StorageVersionError:
            # A future schema is not corruption.  Do not continue to an older
            # backup and never replace the primary during a downgrade.
            raise
        except Exception as exc:
            errors.append(f"{candidate.name}: {exc}")
            continue
        if candidate == source:
            return RecoveryResult(value=value, source=candidate, recovered=False)
        preserved: Path | None = None
        restored = False
        if restore:
            if primary_raw is not None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                preserved = source.with_name(
                    f"{source.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}"
                )
                _write_bytes_without_lock(preserved, primary_raw, backup_count=0)
            _write_bytes_without_lock(source, raw, backup_count=0)
            restored = True
        return RecoveryResult(
            value=value,
            source=candidate,
            recovered=True,
            restored_primary=restored,
            preserved_corrupt=preserved,
        )
    detail = "; ".join(errors) if errors else "no readable file or backup exists"
    raise StorageReadError(f"could not read {source}: {detail}")


def load_json_with_backups(
    path: str | os.PathLike[str],
    *,
    encoding: str = "utf-8-sig",
    backup_count: int = 3,
    restore: bool = False,
    lock_timeout: float = 5.0,
) -> RecoveryResult[object]:
    return load_with_backups(
        path,
        lambda raw: json.loads(raw.decode(encoding)),
        backup_count=backup_count,
        restore=restore,
        lock_timeout=lock_timeout,
    )


@contextmanager
def _file_lock(destination: Path, *, timeout: float) -> Iterator[None]:
    lock_path = destination.with_name(f".{destination.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _lock_stream(stream)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise StorageWriteError(
                        f"timed out waiting for storage lock {lock_path}"
                    ) from exc
                time.sleep(0.025)
        try:
            yield
        finally:
            _unlock_stream(stream)
    finally:
        stream.close()


def _lock_stream(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_stream(stream) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write_bytes_without_lock(
    destination: Path,
    data: bytes,
    *,
    backup_count: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(destination)
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _install_temporary(temporary, destination, backup_count=backup_count)
        temporary = None
    finally:
        if temporary is not None:
            _unlink_if_present(temporary)


def _install_temporary(
    temporary: Path,
    destination: Path,
    *,
    backup_count: int,
    require_absent: bool = False,
) -> None:
    if require_absent:
        if backup_count:
            raise ValueError("an exclusive install cannot rotate backups")
        _install_temporary_exclusive(temporary, destination)
        return

    staged = _stage_backups(destination, backup_count)
    try:
        os.replace(temporary, destination)
    except Exception:
        for _, staged_path in staged:
            _unlink_if_present(staged_path)
        raise

    pending = list(staged)
    try:
        for backup_path, staged_path in staged:
            os.replace(staged_path, backup_path)
            pending.pop(0)
        _fsync_directory(destination.parent)
    except Exception as exc:
        recovery_paths = tuple(path for _, path in pending if path.exists())
        detail = ", ".join(str(path) for path in recovery_paths) or "none"
        raise StorageWriteError(
            f"installed {destination}, but backup/durability finalization failed: {exc}; "
            f"preserved staged recovery files: {detail}",
            final_installed=True,
            recovery_paths=recovery_paths,
        ) from exc


def _install_temporary_exclusive(temporary: Path, destination: Path) -> None:
    """Publish a complete new file without ever replacing an existing name."""

    temporary_metadata = temporary.stat(follow_symlinks=False)
    windows_read_only = os.name == "nt" and bool(
        getattr(temporary_metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    )
    if windows_read_only:
        try:
            # Windows os.rename is same-volume, atomic, and refuses an existing
            # destination.  Unlike deleting a same-file hard-link name, it also
            # succeeds when copystat propagated FILE_ATTRIBUTE_READONLY.
            os.rename(temporary, destination)
        except FileExistsError as exc:
            temporary.chmod(temporary.stat().st_mode | stat.S_IWRITE)
            raise FileExistsError(
                f"exclusive destination already exists: {destination}"
            ) from exc
        except OSError:
            temporary.chmod(temporary.stat().st_mode | stat.S_IWRITE)
            raise
        _fsync_directory(destination.parent)
        return

    try:
        # A same-directory hard link is an atomic create-if-absent operation on
        # supported local filesystems.  Unlike os.replace()/POSIX os.rename(),
        # it cannot overwrite a destination created by a competing process.
        os.link(temporary, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise FileExistsError(
            f"exclusive destination already exists: {destination}"
        ) from exc

    try:
        temporary.unlink()
        _fsync_directory(destination.parent)
    except Exception as exc:
        recovery_paths = (temporary,) if temporary.exists() else ()
        raise StorageWriteError(
            f"installed {destination}, but exclusive durability finalization "
            f"failed: {exc}",
            final_installed=True,
            recovery_paths=recovery_paths,
        ) from exc


def _stage_backups(destination: Path, backup_count: int) -> list[tuple[Path, Path]]:
    if backup_count == 0 or not destination.is_file():
        return []
    sources = [destination]
    sources.extend(_backup_path(destination, index) for index in range(1, backup_count))
    staged: list[tuple[Path, Path]] = []
    try:
        for index, source in enumerate(sources, start=1):
            if not source.is_file():
                continue
            backup_path = _backup_path(destination, index)
            temporary = _temporary_path(backup_path)
            try:
                with (
                    source.open("rb") as input_stream,
                    temporary.open("wb") as output_stream,
                ):
                    shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except Exception:
                _unlink_if_present(temporary)
                raise
            staged.append((backup_path, temporary))
    except Exception:
        for _, staged_path in staged:
            _unlink_if_present(staged_path)
        raise
    return staged


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.bak.{index}")


def _temporary_path(destination: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(raw_path)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        # This helper is used only for best-effort cleanup while another
        # exception is already in flight.  Cleanup must never mask the error
        # that describes whether the final file was installed.
        pass
