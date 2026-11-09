import argparse
import json
import os
from pathlib import Path
from workspace_storage import resolve_storage, StorageVersionError
from .ledger import Ledger, LedgerError, Blocked
from .fixture import Fixture

DEFAULT_STORAGE = Path(__import__('json').loads((Path(__file__).parents[1]/'runtime-plan.json').read_text(encoding='utf-8'))['ledger_root']).parent


def parser():
    p = argparse.ArgumentParser(description="Local fixture and fail-closed publishing intent ledger")
    p.add_argument("--storage-root", type=Path, default=None, help="absolute app storage root; default installation data root")
    sub = p.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("operation_id")
    for field in ("domain", "account", "channel", "manuscript-version", "approval-reference"):
        prepare.add_argument("--" + field, required=True)
    prepare.add_argument("--attachment", action="append", default=[])
    for name in ("begin", "status"):
        sp = sub.add_parser(name)
        sp.add_argument("operation_id", nargs="?" if name == "status" else None)
    uncertain = sub.add_parser("uncertain")
    uncertain.add_argument("operation_id")
    uncertain.add_argument("--reference", required=True)
    rec = sub.add_parser("reconcile")
    rec.add_argument("operation_id")
    rec.add_argument("--status", choices=("VERIFIED", "FAILED_CONFIRMED"), required=True)
    rec.add_argument("--evidence-kind", choices=("post-readback", "publisher-confirmed-absence"), required=True)
    rec.add_argument("--reference", required=True)
    rec.add_argument("--post-id")
    rec.add_argument("--url")
    serve = sub.add_parser("serve")
    serve.add_argument("--state-root", type=Path, required=True, help="absolute fictional run root under installation scratch")
    serve.add_argument("--port", type=int, default=0)
    sub.add_parser("diagnose")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            root = args.state_root
            scratch = Path(__import__('json').loads((Path(__file__).parents[1]/'runtime-plan.json').read_text(encoding='utf-8'))['scratch_root'])
            if not root.is_absolute() or not root.resolve().is_relative_to(scratch.resolve()) or root.resolve() == scratch.resolve():
                raise LedgerError("fixture --state-root must be an explicit owned task/run path under installation scratch")
            if not 0 <= args.port <= 65535:
                raise LedgerError("invalid port")
            server = Fixture(root).server(args.port)
            print(json.dumps({"fixture": f"http://127.0.0.1:{server.server_port}/", "state_root": str(root), "device": "CPU", "fictional": True}), flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        root = args.storage_root or Path(os.environ.get("BROWSEROS_NEO_STORAGE_ROOT", str(DEFAULT_STORAGE)))
        storage = resolve_storage("browseros-neo", app_name="browseros-neo", override=root, create=False)
        ledger = Ledger(storage.directory("data", "posting-ledger.json"))
        if args.command == "prepare":
            output = ledger.prepare(args.operation_id, domain=args.domain, account=args.account, channel=args.channel, manuscript_version=args.manuscript_version, attachments=args.attachment, approval_reference=args.approval_reference)
        elif args.command == "begin":
            output = ledger.begin(args.operation_id)
        elif args.command == "status":
            output = ledger.status(args.operation_id)
        elif args.command == "uncertain":
            output = ledger.uncertain(args.operation_id, args.reference)
        elif args.command == "reconcile":
            output = ledger.reconcile(args.operation_id, args.status, verification={"kind": args.evidence_kind, "reference": args.reference}, post_id=args.post_id, url=args.url)
        else:
            output = {"storage_root": str(root), "ledger": str(ledger.path), "schema_version": 1, "device": "CPU", "gpu_jit_tensorrt": "NOT_APPLICABLE: browser and HTTP I/O only"}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Blocked as exc:
        print(json.dumps({"blocked": str(exc), "operation": exc.operation}, ensure_ascii=False))
        return 3
    except (LedgerError, StorageVersionError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
