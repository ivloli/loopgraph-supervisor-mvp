import argparse
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def call(base: str, method: str, path: str, payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(f"{base.rstrip('/')}{path}", data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode()
        raise SystemExit(f"HTTP {error.code}: {detail}") from error


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="LoopGraph Supervisor HTTP client")
    root.add_argument("--url", default="http://127.0.0.1:8080")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    for name in ("status", "pause", "resume"):
        item = commands.add_parser(name)
        item.add_argument("workflow_id")
    start = commands.add_parser("start")
    start.add_argument("workflow_id")
    start.add_argument("goal")
    start.add_argument("--workspace", required=True)
    start.add_argument("--verify", action="append", required=True)
    start.add_argument("--allow", action="append", required=True)
    start.add_argument("--max-attempts", type=int, default=3)
    hitl = commands.add_parser("hitl")
    hitl.add_argument("workflow_id")
    hitl.add_argument("decision", choices=("approve", "retry", "reject"))
    rollback = commands.add_parser("rollback")
    rollback.add_argument("workflow_id")
    rollback.add_argument("version_id")
    recover = commands.add_parser("recover")
    recover.add_argument("workflow_id")
    recover.add_argument("action", choices=("verify-existing", "retry-same-attempt", "restore-baseline", "abort-preserve"))
    evolve = commands.add_parser("evolve")
    evolve.add_argument("target_id")
    evolve.add_argument("comment")
    evolve.add_argument("--reviewer", required=True)
    return root


def main():
    args = parser().parse_args()
    if args.command == "evolve":
        result = call(args.url, "POST", "/evolution/triggers", {"target_id": args.target_id, "reviewer": args.reviewer, "comment": args.comment})
    elif args.command == "list":
        result = call(args.url, "GET", "/workflows")
    elif args.command == "status":
        result = call(args.url, "GET", f"/workflows/{args.workflow_id}")
    elif args.command == "start":
        result = call(args.url, "POST", "/workflows", {"id": args.workflow_id, "goal": args.goal, "max_attempts": args.max_attempts, "acceptance": {"workspace": args.workspace, "commands": args.verify, "allowed_files": args.allow}})
    elif args.command in ("pause", "resume"):
        result = call(args.url, "POST", f"/workflows/{args.workflow_id}/{args.command}", {})
    elif args.command == "hitl":
        result = call(args.url, "POST", f"/workflows/{args.workflow_id}/hitl", {"decision": args.decision})
    elif args.command == "recover":
        result = call(args.url, "POST", f"/workflows/{args.workflow_id}/recover", {"action": args.action})
    else:
        result = call(args.url, "POST", f"/workflows/{args.workflow_id}/rollback", {"version_id": args.version_id})
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
