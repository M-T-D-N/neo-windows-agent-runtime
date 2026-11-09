"""Build child environments without changing the process or path values."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping


def normalize_environment(
    source: Mapping[str, str],
    *,
    windows: bool | None = None,
    prefer_uppercase_path: bool = False,
) -> dict[str, str]:
    """Reject conflicting aliases within one layer; retain identical aliases once.

    prefer_uppercase_path preserves the shared native launcher's legacy rule.
    It only resolves PATH aliases when an exact uppercase PATH was supplied.
    """
    if not isinstance(source, Mapping):
        raise TypeError("environment must be a mapping")
    windows = os.name == "nt" if windows is None else windows
    result: dict[str, str] = {}
    for name, value in source.items():
        if not isinstance(name, str) or not name or "\0" in name or "=" in name:
            raise ValueError("invalid environment variable name")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"invalid environment value for {name!r}")
        key = name.upper() if windows else name
        if windows and key == "PATH" and prefer_uppercase_path and "PATH" in source:
            value = source["PATH"]
        if key in result and result[key] != value:
            raise ValueError(f"conflicting environment aliases for {key!r}")
        result[key] = value
    return result


def build_environment(
    base: Mapping[str, str],
    overrides: Mapping[str, str] | None = None,
    *,
    remove: Iterable[str] = (),
    prepend_path: Iterable[str] = (),
    windows: bool | None = None,
) -> dict[str, str]:
    """Normalize before reading; apply removal, explicit overrides, then prepend.

    Set PATH in overrides for replacement. Omit it to preserve inherited PATH.
    Registry lookup, runtime selection and path deduplication belong to callers.
    """
    windows = os.name == "nt" if windows is None else windows
    if isinstance(remove, str) or isinstance(prepend_path, str):
        raise TypeError("environment operations require lists of names or paths")
    env = normalize_environment(base, windows=windows)
    for name in remove:
        key = next(iter(normalize_environment({name: ""}, windows=windows)))
        env.pop(key, None)
    env.update(
        normalize_environment({} if overrides is None else overrides, windows=windows)
    )
    separator = ";" if windows else ":"
    entries = list(prepend_path)
    for entry in entries:
        if (
            not isinstance(entry, str)
            or not entry
            or "\0" in entry
            or separator in entry
        ):
            raise ValueError("invalid PATH prepend entry")
    if entries:
        inherited = env.get("PATH", "")
        env["PATH"] = separator.join(entries) + (
            separator + inherited if inherited else ""
        )
    return env
