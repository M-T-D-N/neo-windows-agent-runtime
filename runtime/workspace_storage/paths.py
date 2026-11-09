from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from typing import Mapping


GLOBAL_STORAGE_ENV = "MY_PYTHON_PROJECT_STORAGE_ROOT"
_KINDS = ("config", "data", "cache", "logs", "temp", "exports")
_WINDOWS_INVALID_NAME_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class StoragePathError(ValueError):
    """Raised when an application identifier or storage path is unsafe."""


@dataclass(frozen=True, slots=True)
class AppStorage:
    """Resolved storage directories for one application.

    ``legacy-flat`` is a compatibility layout: config and data intentionally
    share an existing application root while cache/log/temp/export get named
    subdirectories.  New projects should use the standard layout.
    """

    app_id: str
    app_name: str
    config: Path
    data: Path
    cache: Path
    logs: Path
    temp: Path
    exports: Path
    layout: str
    override_source: str | None = None
    legacy_roots: tuple[Path, ...] = ()

    def directory(self, kind: str, *parts: str | os.PathLike[str]) -> Path:
        if kind not in _KINDS:
            raise StoragePathError(f"unknown storage kind: {kind}")
        root = getattr(self, kind)
        return root.joinpath(*(_validate_relative_path(part) for part in parts))

    def ensure(self, *kinds: str) -> "AppStorage":
        selected = kinds or _KINDS
        for kind in selected:
            self.directory(kind).mkdir(parents=True, exist_ok=True)
        return self

    def discover_legacy(self, *required_names: str) -> tuple[Path, ...]:
        required_paths = tuple(_validate_relative_path(name) for name in required_names)
        discovered: list[Path] = []
        for root in self.legacy_roots:
            if not root.is_dir():
                continue
            if required_paths and not any(
                (root / name).exists() for name in required_paths
            ):
                continue
            discovered.append(root)
        return tuple(discovered)

    def diagnostics(self) -> dict[str, object]:
        directories: dict[str, dict[str, object]] = {}
        for kind in _KINDS:
            path = self.directory(kind)
            ancestor = _nearest_existing_ancestor(path)
            directories[kind] = {
                "path": str(path),
                "exists": path.is_dir(),
                "existing_ancestor": str(ancestor),
                "ancestor_writable": os.access(ancestor, os.W_OK),
            }
        return {
            "schema_version": 1,
            "app_id": self.app_id,
            "app_name": self.app_name,
            "layout": self.layout,
            "override_source": self.override_source,
            "directories": directories,
            "legacy_roots": [str(path) for path in self.legacy_roots],
            "legacy_roots_found": [str(path) for path in self.discover_legacy()],
        }


def resolve_storage(
    app_id: str,
    *,
    app_name: str | None = None,
    override: str | os.PathLike[str] | None = None,
    env_var: str | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: str | os.PathLike[str] | None = None,
    legacy_flat_root: str | os.PathLike[str] | None = None,
    legacy_roots: tuple[str | os.PathLike[str], ...] = (),
    create: bool = False,
) -> AppStorage:
    """Resolve standard per-user directories without consulting the CWD.

    Precedence is explicit override, application environment variable, global
    workspace environment variable, compatibility root, then OS defaults.
    An application override is the app root.  The global override is a shared
    base and therefore receives the application directory as a child.
    """

    normalized_id = _validate_app_id(app_id)
    display_name = _validate_app_name(app_name or normalized_id)
    env = os.environ if environ is None else environ
    app_env = env_var or _default_env_var(normalized_id)
    selected_override: Path | None = None
    override_source: str | None = None

    if override is not None:
        selected_override = _coerce_path(override)
        override_source = "argument"
    elif env.get(app_env):
        selected_override = _coerce_path(env[app_env])
        override_source = app_env
    elif env.get(GLOBAL_STORAGE_ENV):
        selected_override = _coerce_path(env[GLOBAL_STORAGE_ENV]) / display_name
        override_source = GLOBAL_STORAGE_ENV

    legacy = tuple(_coerce_path(path) for path in legacy_roots)
    if selected_override is not None:
        storage = _root_layout(
            normalized_id,
            display_name,
            selected_override,
            layout="override",
            override_source=override_source,
            legacy_roots=legacy,
        )
    elif legacy_flat_root is not None:
        root = _coerce_path(legacy_flat_root)
        storage = AppStorage(
            app_id=normalized_id,
            app_name=display_name,
            config=root,
            data=root,
            cache=root / "cache",
            logs=root / "logs",
            temp=root / "temp",
            exports=root / "exports",
            layout="legacy-flat",
            legacy_roots=_deduplicate_paths((*legacy, root)),
        )
    else:
        storage = _platform_layout(
            normalized_id,
            display_name,
            env,
            platform or sys.platform,
            _coerce_path(home) if home is not None else Path.home(),
            legacy,
        )
    if create:
        storage.ensure()
    return storage


def _platform_layout(
    app_id: str,
    app_name: str,
    env: Mapping[str, str],
    platform: str,
    home: Path,
    legacy_roots: tuple[Path, ...],
) -> AppStorage:
    key = platform.lower()
    if key.startswith("win"):
        local = _coerce_path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return _root_layout(
            app_id, app_name, local / app_name, legacy_roots=legacy_roots
        )
    if key == "darwin":
        support = home / "Library" / "Application Support" / app_name
        return AppStorage(
            app_id=app_id,
            app_name=app_name,
            config=support / "config",
            data=support / "data",
            cache=home / "Library" / "Caches" / app_name,
            logs=home / "Library" / "Logs" / app_name,
            temp=home / "Library" / "Caches" / app_name / "temp",
            exports=support / "exports",
            layout="standard-macos",
            legacy_roots=legacy_roots,
        )

    config_home = _coerce_path(env.get("XDG_CONFIG_HOME") or home / ".config")
    data_home = _coerce_path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    cache_home = _coerce_path(env.get("XDG_CACHE_HOME") or home / ".cache")
    state_home = _coerce_path(env.get("XDG_STATE_HOME") or home / ".local" / "state")
    runtime_home = env.get("XDG_RUNTIME_DIR")
    return AppStorage(
        app_id=app_id,
        app_name=app_name,
        config=config_home / app_name,
        data=data_home / app_name,
        cache=cache_home / app_name,
        logs=state_home / app_name / "logs",
        temp=(
            _coerce_path(runtime_home) / app_name
            if runtime_home
            else cache_home / app_name / "temp"
        ),
        exports=data_home / app_name / "exports",
        layout="standard-xdg",
        legacy_roots=legacy_roots,
    )


def _root_layout(
    app_id: str,
    app_name: str,
    root: Path,
    *,
    layout: str = "standard-windows",
    override_source: str | None = None,
    legacy_roots: tuple[Path, ...] = (),
) -> AppStorage:
    return AppStorage(
        app_id=app_id,
        app_name=app_name,
        config=root / "config",
        data=root / "data",
        cache=root / "cache",
        logs=root / "logs",
        temp=root / "temp",
        exports=root / "exports",
        layout=layout,
        override_source=override_source,
        legacy_roots=legacy_roots,
    )


def _validate_app_id(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized):
        raise StoragePathError(
            "app_id must contain only lowercase letters, digits, dot, underscore, or hyphen"
        )
    return normalized


def _validate_app_name(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or value.endswith(".")
        or any(character in _WINDOWS_INVALID_NAME_CHARACTERS for character in value)
        or any(ord(character) < 32 for character in value)
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise StoragePathError("app_name must be one safe directory name")
    return value


def _default_env_var(app_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", app_id.upper()) + "_STORAGE_ROOT"


def _coerce_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise StoragePathError(f"storage path must be absolute: {value!s}")
    return path


def _validate_relative_path(value: str | os.PathLike[str]) -> Path:
    raw_value = os.fspath(value)
    if not isinstance(raw_value, str) or not raw_value:
        raise StoragePathError("storage path part cannot be empty")
    native = Path(raw_value)
    variants = (native, PurePosixPath(raw_value), PureWindowsPath(raw_value))
    if any(path.is_absolute() or path.anchor or path.drive for path in variants):
        raise StoragePathError(f"storage path part must be relative: {raw_value}")
    if any(part == ".." for path in variants for part in path.parts):
        raise StoragePathError(f"storage path part cannot escape its root: {raw_value}")
    return native


def _nearest_existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _deduplicate_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in paths:
        if path not in result:
            result.append(path)
    return tuple(result)
