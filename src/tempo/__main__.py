"""tempo command line: hook, render, init."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from tempo import __version__

HOSTS = ("claude", "copilot", "codex")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tempo")
    parser.add_argument("--version", action="version", version=f"tempo {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook", help="run a host hook (payload on stdin)")
    hook.add_argument("host", choices=HOSTS)
    hook.add_argument("event")

    render = sub.add_parser("render", help="regenerate the per-host governing layout")
    render.add_argument("--root", type=Path, default=Path.cwd())
    render.add_argument("--check", action="store_true", help="fail if any rendered file is stale")

    init = sub.add_parser("init", help="install the governing layout into a host repo")
    init.add_argument("--root", type=Path, default=Path.cwd())
    init.add_argument("--host", choices=(*HOSTS, "all"), action="append")
    init.add_argument("--force", action="store_true", help="overwrite differing non-mergeable files")

    sub.add_parser("mcp", help="serve the orchestrator tool surface over stdio (AGENT_PLAN.md phase T3)")

    args = parser.parse_args(argv)
    if args.command == "hook":
        from tempo.hooks import handle

        out, code = handle(args.host, args.event, json.loads(sys.stdin.read() or "{}"), os.environ)
        if out:
            print(out)
        return code
    if args.command == "render":
        from tempo.render import write

        drifted = write(args.root.resolve(), check=args.check)
        for path in drifted:
            print(("stale " if args.check else "wrote ") + path)
        return 1 if args.check and drifted else 0
    if args.command == "init":
        from tempo.init import main as init_main

        hosts = set(HOSTS) if not args.host or "all" in args.host else set(args.host)
        return init_main(args.root.resolve(), hosts, args.force)
    print("tempo mcp is not built yet (AGENT_PLAN.md phase T3).", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
