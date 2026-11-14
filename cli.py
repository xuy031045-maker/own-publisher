import argparse
import json
import sys

from browser import account_session
from publisher import PLATFORMS, acknowledge_verification, add_account, batch_status, cancel_verification, list_accounts, prepare_batch, run_batch


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
    commands.add_parser("continue", help="通知正在等待验证的批次继续检查").add_argument("batch")
    commands.add_parser("cancel-verification", help="取消正在等待的人工验证").add_argument("batch")
    run = commands.add_parser("publish")
    run.add_argument("batch")
    run.add_argument("--confirm", required=True)
    run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--interactive", action="store_true", help="打开浏览器以便处理登录或验证")
    run.add_argument("--allow-duplicate-account", action="append", default=[], metavar="ALIAS",
                     help="确认该账号需要再次发布相同图文；可重复指定")
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
        elif args.command == "continue":
            result = acknowledge_verification(args.batch)
        elif args.command == "cancel-verification":
            result = cancel_verification(args.batch)
        else:
            def verification(alias, detail):
                if detail:
                    print(f"{alias}：{detail} 完成后可在工作台记录页点击继续，或另开窗口运行 .\\发布台.ps1 continue {args.batch}。", file=sys.stderr, flush=True)
            options = {"retry": args.retry_failed, "on_verification": verification if args.interactive else None}
            if args.allow_duplicate_account:
                options["allow_duplicate_accounts"] = args.allow_duplicate_account
            result = run_batch(args.batch, args.confirm, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
