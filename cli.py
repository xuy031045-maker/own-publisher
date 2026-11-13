import argparse
import json
import sys

from browser import account_session
from publisher import PLATFORMS, add_account, batch_status, list_accounts, prepare_batch, run_batch


def main():
    parser = argparse.ArgumentParser(description="自有发布台")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add-account")
    add.add_argument("alias")
    add.add_argument("platform", choices=PLATFORMS)
    add.add_argument("--label", default="")
    commands.add_parser("accounts")
    for action in ("login", "check"):
        commands.add_parser(action).add_argument("alias")
    commands.add_parser("preview").add_argument("manifest")
    status = commands.add_parser("status")
    status.add_argument("batch")
    run = commands.add_parser("publish")
    run.add_argument("batch")
    run.add_argument("--confirm", required=True)
    run.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "add-account":
            result = add_account(args.alias, args.platform, args.label)
        elif args.command == "accounts":
            result = list_accounts()
        elif args.command in {"login", "check"}:
            result = account_session(args.alias, login=args.command == "login")
        elif args.command == "preview":
            result = prepare_batch(args.manifest)
        elif args.command == "status":
            result = batch_status(args.batch)
        else:
            result = run_batch(args.batch, args.confirm, retry=args.retry_failed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
