from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .migration import copy_legacy_tree
from .paths import resolve_storage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or migrate workspace storage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnose = subparsers.add_parser("diagnose", help="print resolved app directories")
    diagnose.add_argument("app_id")
    diagnose.add_argument("--app-name")
    diagnose.add_argument("--override", type=Path)
    diagnose.add_argument("--env-var")
    diagnose.add_argument("--legacy-flat-root", type=Path)
    migrate = subparsers.add_parser(
        "migrate-tree", help="copy a legacy tree without deleting it"
    )
    migrate.add_argument("source", type=Path)
    migrate.add_argument("destination", type=Path)
    migrate.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "diagnose":
        try:
            storage = resolve_storage(
                args.app_id,
                app_name=args.app_name,
                override=args.override,
                env_var=args.env_var,
                legacy_flat_root=args.legacy_flat_root,
            )
        except (OSError, ValueError) as exc:
            print(f"Storage policy error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(storage.diagnostics(), ensure_ascii=False, indent=2))
        return 0
    try:
        report = copy_legacy_tree(args.source, args.destination, execute=args.execute)
    except (OSError, ValueError) as exc:
        print(f"Storage migration error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 1 if report.conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
