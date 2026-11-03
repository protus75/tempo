"""The CLI module entry (TEMPO_PLAN.md section 18): `uv run python -m tempo <verb>`.

The composition root: the only place real seams are constructed. Exit 2 is an engine-plane refusal.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tempo import hooks, hostfiles, hosts, render as host_render
from tempo.checkout import SPECS_DIR, Checkout, Refusal
from tempo.config import ConfigError, load_config, migrate_config
from tempo.enginelog import EngineLog
from tempo.git import Git
from tempo.journal import Journal, JournalCorruption
from tempo.lockfile import LockHeld
from tempo.notify import NOTIFY_TIMEOUT_S
from tempo.redact import Redactor
from tempo.seams import Clock, CommandNotifications, ExecutableNotFound, LocalFileSystem, SubprocessExec, detached_spawn
from tempo.status import project, render
from tempo.tickets import IntakeRefused, TicketInvalid, stem_findings, template, ticket_path, validate_ticket
from tempo.tools import Tools

GIT_TIMEOUT_S = 30.0
EXIT_REFUSED = 2


def _clock() -> datetime:
    return datetime.now(UTC)


def build_tools(checkout: Checkout, *, config_path: Path | None) -> Tools:
    """The orchestrator tools, with job workers launched as detached `tempo job-run <id>` processes."""
    def launch(id: str, fd: int) -> None:
        config = ["--config", str(config_path)] if config_path is not None else []
        detached_spawn([sys.executable, "-m", "tempo", *config, "job-run", id], cwd=checkout.repo,
                       env=dict(checkout.env), keep_fd=fd)

    notifications = CommandNotifications(SubprocessExec(), cwd=checkout.repo, env=dict(checkout.env),
                                         timeout=NOTIFY_TIMEOUT_S, scrub=Redactor({}).scrub)
    return Tools(checkout, launch=launch, notifications=notifications)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tempo")
    ap.add_argument("--config", type=Path, help="config path (default: config.yaml at the checkout root)")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("status", help="project current state from the journal (read-only)")
    sub.add_parser("doctor", help="inspect setup without changing state")
    sub.add_parser("core", help="refresh managed conduct in every routed agent's root file")
    sub.add_parser("migrate-config", help="validate and migrate schema_version: 0 to the current config schema")
    new = sub.add_parser("new", help="template tickets/<stem>/ticket.md and lint it")
    new.add_argument("stem")
    tool = sub.add_parser("tool", help="run one orchestrator tool and print its JSON result")
    tool.add_argument("name")
    tool.add_argument("args", nargs="?", default="{}", help="the tool's arguments as one JSON object")
    sub.add_parser("hook", help="run one host hook on the JSON payload from stdin").add_argument("host")
    sub.add_parser("render", help="write the generated host files").add_argument(
        "--check", action="store_true", help="only list the files that differ from a fresh render")
    sub.add_parser("job-run", help="(internal) the detached worker of one tool job").add_argument("id")
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    clock: Clock = _clock,
    stdin: bytes | None = None,
) -> int:
    args = _parser().parse_args(argv)
    repo = Path(cwd) if cwd is not None else Path.cwd()
    env = dict(os.environ) if env is None else env
    if args.verb == "hook":
        return _hook(args.host, sys.stdin.buffer.read() if stdin is None else stdin, repo, clock)
    try:
        if args.verb == "migrate-config":
            migrate_config(args.config, cwd=repo, fs=LocalFileSystem())
            return 0
        if args.verb == "new":
            return _new(repo, args.stem)
        if args.verb == "doctor":
            from tempo import doctor
            results = asyncio.run(doctor.check(repo=repo, config_path=args.config, env=env,
                exec_=SubprocessExec(), clock=clock, in_venv=sys.prefix != sys.base_prefix))
            print(doctor.render(results), end="")
            return 0 if all(r.passed for r in results) else 2
        config = load_config(args.config, cwd=repo)
        if args.verb == "core":
            hostfiles.refresh(hostfiles.conduct_paths(config, repo), LocalFileSystem())
            return 0
        if args.verb == "render":
            if args.check:
                drift = host_render.drift(repo, config)
                print("\n".join(f"stale: {rel}" for rel in drift) if drift else "render: current")
                return EXIT_REFUSED if drift else 0
            print("\n".join(f"wrote: {rel}" for rel in host_render.write(repo, config, LocalFileSystem()))
                  or "render: current")
            return 0
        exec_ = SubprocessExec()
        checkout = Checkout(
            repo=repo, config=config, env=env, exec_=exec_,
            git=Git(exec_, env=dict(env), timeout=GIT_TIMEOUT_S),
            journal=Journal(config.state_dir, clock), fs=LocalFileSystem(), clock=clock,
        )
        if args.verb == "status":
            print(render(asyncio.run(_status(checkout))), end="")
            return 0
        if args.verb == "tool":
            return _tool(build_tools(checkout, config_path=args.config), args.name, args.args)
        if args.verb == "job-run":
            asyncio.run(build_tools(checkout, config_path=args.config).run_job(args.id))
            return 0
        raise AssertionError(f"unrouted verb {args.verb}")
    except (ConfigError, Refusal, LockHeld, IntakeRefused, JournalCorruption,
            ExecutableNotFound, hostfiles.MarkerCorruption) as e:
        print(f"tempo {args.verb}: {e}", file=sys.stderr)
        return EXIT_REFUSED


def _tool(tools: Tools, name: str, raw: str) -> int:
    """Print one JSON object: `ok` true with the result, or false with the refusal and its paved road."""
    def refuse(message: str, road: str) -> int:
        print(json.dumps({"ok": False, "refused": message, "do_instead": road}))
        return EXIT_REFUSED
    try:
        args = json.loads(raw)
    except ValueError as e:
        return refuse(f"the arguments are not JSON: {e}", "pass one JSON object in single quotes")
    if not isinstance(args, dict):
        return refuse("the arguments are not a JSON object", "pass one JSON object in single quotes")
    try:
        result = asyncio.run(tools.run(name, args))
    except (Refusal, LockHeld, IntakeRefused, ConfigError) as e:
        message, _, road = str(e).rpartition(" -- ")
        return refuse(message or road, road if message else "see the message")
    print(json.dumps({"ok": True, **result}))
    return 0


def _hook(host: str, data: bytes, repo: Path, clock: Clock) -> int:
    """Hooks fail closed: any fault denies, on every host. Every payload and its reply go to the engine log,
    so a denied unparseable payload can be read back."""
    try:
        out, code = hooks.handle(host, data, cwd=repo, clock=clock, fs=LocalFileSystem())
        if (root := hooks.find_repo(repo)) is not None:
            EngineLog(load_config(None, cwd=root).state_dir / "engine.log", Redactor({}), clock).event(
                "hook", host=host, payload=data.decode(errors="replace"), reply=out, exit=code)
    except Exception as exc:
        out, code = hosts.reply(False, f"tempo hook failed: {type(exc).__name__}: {exc}"), 2
    if out:
        print(out)
    if code:
        print(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"], file=sys.stderr)
    return code


async def _status(checkout: Checkout):
    from tempo.box import BOX_DIR, Box, RereportJournal
    from tempo.retro import production_evidence, project_window
    from tempo.specs import load_spec

    events = checkout.journal.read()
    window = project_window(events)
    await production_evidence(window, checkout)
    spec = load_spec((SPECS_DIR / "retro.md").read_text())
    provenance = {"head": await checkout.git.rev_parse(checkout.repo, "HEAD"),
                  "spec_version": spec.meta.version, "at": checkout.clock().isoformat()}
    return project(events, window=window, provenance=provenance,
        box=Box(checkout.config.state_dir / BOX_DIR, checkout.fs,
                rereport=RereportJournal(checkout.journal)), policy=checkout.config.box_policy)


def _new(repo: Path, stem: str) -> int:
    """Author the ticket file only: authorship is outside the lock fence; intake commits it when the ticket starts."""
    if findings := stem_findings(stem):
        raise Refusal(findings[0].message, findings[0].paved_road)
    rel = ticket_path(stem)
    if (repo / rel).exists():
        raise Refusal(f"{rel} already exists", "edit it in place, or pick a new stem")
    text = template()
    LocalFileSystem().write(repo / rel, text.encode())
    print(f"authored {rel}")
    try:
        validate_ticket(stem, text, repo)
    except TicketInvalid as e:
        print(f"fill these before {stem} runs:")
        for f in e.findings:
            print(f"- {f.message} ({f.paved_road})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
