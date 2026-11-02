"""The CLI module entry (TEMPO_PLAN.md section 18): `uv run python -m tempo <verb>`.

The composition root: the only place real seams are constructed. Exit 2 is an engine-plane refusal.
"""

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence, Set
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from tempo import control, drain, hostfiles, runner, serve, triage
from tempo.checkout import SPECS_DIR, Checkout, Refusal, call_timeout
from tempo.config import ConfigError, ConfigSnapshot, load_config, migrate_config
from tempo.control import PauseConsumer
from tempo.daemon import DaemonCore, daemon_core
from tempo.daemon import StartupBoundary
from tempo.git import Git
from tempo.journal import Journal, JournalCorruption
from tempo.lockfile import LockHeld, Lockfile
from tempo.notify import NOTIFY_TIMEOUT_S, NotificationReconciler
from tempo.providers import ProviderLLM, ProviderSession, ProviderSetupError, routed_conduct_paths, child_env
from tempo.redact import Redactor
from tempo.restart import Restart
from tempo.seams import Clock, ExecutableNotFound, LocalFileSystem, SubprocessExec
from tempo.seams import CommandNotifications
from tempo.status import project, render
from tempo.storm import StormLedger
from tempo.timers import Timers
from tempo.tickets import IntakeRefused, Ticket, TicketInvalid, stem_findings, template, ticket_path, validate_ticket

GIT_TIMEOUT_S = 30.0


def _clock() -> datetime:
    return datetime.now(UTC)


def build_control(checkout: Checkout) -> PauseConsumer:
    """Compose without reading requests, publishing discovery, or starting tasks."""
    from tempo.storm import storm_producer

    box = storm_producer(root=checkout.config.state_dir / "box", fs=checkout.fs,
                         journal=checkout.journal, clock=checkout.clock)
    consumer = PauseConsumer(journal=checkout.journal, lifecycle_id=uuid4().hex,
                         state_dir=checkout.config.state_dir, fs=checkout.fs, sleep=checkout.sleep,
                         files=lambda: (checkout.config.state_dir / "control/inbox").glob("*"),
                         read=Path.read_bytes, recover=box.recover,
                         storm_holds=StormLedger(journal=checkout.journal, clock=checkout.clock).holds)
    timers = Timers(journal=checkout.journal, clock=checkout.clock, sleep=checkout.sleep)
    consumer.providers = ProviderSession(checkout.config, journal=checkout.journal,
        clock=checkout.clock, sleep=checkout.sleep, timers=timers)
    consumer.load_config = lambda: load_config(None, cwd=checkout.repo)
    return consumer


def build_daemon_core(
    checkout: Checkout, *, config_path: Path | None = None,
    read: Callable[[str], str | None], debounce: float,
    quarantined: Callable[[], Set[str]], drought_parked: Callable[[], Set[str]],
    completed_unmerged: Callable[[], int],
    prepare: Callable[[Checkout], Awaitable[runner.Dispatch]] = runner.prepare_pipeline,
) -> DaemonCore:
    """Compose the production core without starting consumers or preparing a dispatch."""
    consumer = checkout.control or build_control(checkout)
    consumer.load_config = lambda: load_config(config_path, cwd=checkout.repo)
    checkout = replace(checkout, control=consumer)

    def bind(snapshot: ConfigSnapshot) -> runner.Dispatch:
        local = replace(checkout, config=snapshot)

        async def dispatch(ticket: Ticket) -> str:
            callback = await prepare(local)
            tier, _ = runner.capability(ticket, checkout.journal.read())
            drought = (consumer.providers.drought(snapshot, tier, 'implement')
                       if consumer.providers.active else None)
            if drought:
                consumer.providers.park(ticket.stem, drought)
                core.scheduler.update(ticket)
                return 'infra_error'
            result = await callback(ticket)
            history = checkout.journal.read()
            body = next((e.body for e in reversed(history)
                if e.type == 'state_transition' and e.ticket == ticket.stem), {})
            if 'provider_drought' in body:
                core.scheduler.update(ticket)
            return result

        return dispatch

    core = daemon_core(
        checkout.repo, journal=checkout.journal,
        load=lambda: load_config(config_path, cwd=checkout.repo), bind=bind, read=read,
        clock=checkout.clock, sleep=checkout.sleep, debounce=debounce,
        quarantined=quarantined, drought_parked=drought_parked,
        completed_unmerged=completed_unmerged, max_unmerged=checkout.config.scheduler.max_unmerged,
        before_dispatch=consumer.checkpoint, control=consumer,
    )
    timers = consumer.providers.timers
    restart = Restart(checkout, timers=timers,
                      owned=lambda: core.admission.active is not None or core.admission.task is not None)
    core.admission.restart = restart
    core.admission._before_dispatch = StartupBoundary(restart, consumer).checkpoint
    def provider_holds():
        held = set(drought_parked())
        latest = {}
        for event in checkout.journal.read():
            if event.type == 'state_transition' and event.ticket is not None:
                latest[event.ticket] = event.body
        for stem, body in latest.items():
            if route := body.get('provider_drought'):
                snapshot = load_config(config_path, cwd=checkout.repo)
                if consumer.providers.drought(snapshot, route['tier'], route['surface']):
                    held.add(stem)
        return held
    core.scheduler.drought_parked = provider_holds
    return core


def build_serve(checkout: Checkout, *, config_path=None, read=None,
                stems=None, prepare=runner.prepare_pipeline, signals=None, failure=None) -> serve.Serve:
    """Compose filesystem delivery at the same root as every other real seam."""
    def read_ticket(stem):
        path = checkout.repo / ticket_path(stem)
        return path.read_text() if path.is_file() else None
    return serve.Serve(checkout, config_path=config_path, read=read or read_ticket,
        stems=stems or (lambda: (p.parent.name for p in (checkout.repo / "tickets").glob("*/ticket.md"))),
        prepare=prepare, signals=signals, failure=failure)


def build_notifications(checkout: Checkout, *, config_path: Path | None, log) -> NotificationReconciler:
    executor = SubprocessExec()  # Never alias active work.
    def compose(config):
        return CommandNotifications(executor, cwd=checkout.repo,
            env=child_env(checkout.env, config, serving=None), timeout=NOTIFY_TIMEOUT_S,
            scrub=Redactor.from_config(config, checkout.env).scrub)
    return NotificationReconciler(journal=checkout.journal, clock=checkout.clock,
        config_path=config_path or checkout.repo / "config.yaml",
        load=lambda: load_config(config_path, cwd=checkout.repo), compose=compose, log=log)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m tempo")
    ap.add_argument("--config", type=Path, help="config path (default: config.yaml at the checkout root)")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("status", help="project current state from the journal (read-only)")
    sub.add_parser("retro", help="force a retro report under the writer lock")
    sub.add_parser("doctor", help="inspect setup without changing state")
    sub.add_parser("core", help="refresh managed conduct in every routed agent's root file")
    sub.add_parser("migrate-config", help="validate and migrate schema_version: 0 to the current config schema")
    for verb in ("pause", "resume"):
        sub.add_parser(verb, help=f"submit {verb} to the running engine, or do nothing under the idle lock")
    sub.add_parser("kill", help="submit kill to the running engine; refuse when nothing is running")
    sub.add_parser("triage", help="make one sequential pass over pending suggestions under the lock")
    new = sub.add_parser("new", help="template tickets/<stem>/ticket.md and lint it")
    new.add_argument("stem")
    run = sub.add_parser("run", help="drive one ticket through intake and the stage pipeline under the lock")
    run.add_argument("stem")
    for verb in ("confirm", "reject"):
        sub.add_parser(verb, help=f"{verb} a draft or Reject item under the lock").add_argument("stem")
    sub.add_parser("drain", help="run every eligible ticket to quiescence under the lock")
    sub.add_parser("serve", help="run the foreground daemon until kill or a signal stops it")
    return ap


def main(
    argv: Sequence[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    clock: Clock = _clock,
    pipeline: runner.Pipeline = runner.pipeline,
) -> int:
    args = _parser().parse_args(argv)
    repo = Path(cwd) if cwd is not None else Path.cwd()
    env = dict(os.environ) if env is None else env
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
            hostfiles.refresh(routed_conduct_paths(config, repo), LocalFileSystem())
            return 0
        exec_ = SubprocessExec()
        checkout = Checkout(
            repo=repo, config=config, env=env, exec_=exec_,
            git=Git(exec_, env=child_env(env, config), timeout=GIT_TIMEOUT_S),
            journal=Journal(config.state_dir, clock), fs=LocalFileSystem(), clock=clock,
        )
        if args.verb == "status":
            print(render(asyncio.run(_status(checkout))), end="")
            return 0
        if args.verb in {"pause", "resume", "kill"}:
            return asyncio.run(_control(checkout, args.verb))
        if args.verb in {"confirm", "reject"}:
            return asyncio.run(_verdict(checkout, args.stem, args.verb))
        if args.verb == "triage":
            async def one_pass() -> list[tuple[str, str]]:
                lock = Lockfile(config.state_dir, instance_id=await checkout.git.describe(repo), clock=clock)
                lock.acquire()
                session = build_control(checkout).providers
                try:
                    llm = ProviderLLM(config, exec_=exec_, fs=checkout.fs, env=env,
                                      cwd=repo, timeout=call_timeout(config),
                                      session=session)
                    session.timers.reconstruct()
                    session.timers.fire_due()
                    problems = await llm.preflight() if session.active else []
                    if problems:
                        raise ProviderSetupError('; '.join(problems))
                    return await triage.triage_pass(checkout, llm)
                finally:
                    try:
                        await session.close()
                    finally:
                        lock.release()

            lines = asyncio.run(one_pass())
            print("\n".join(line for _, line in lines) if lines else "no pending suggestions")
            return 0
        if args.verb == "serve":
            return asyncio.run(serve.serve(checkout, config_path=args.config, signals=_serve_signals))
        checkout = replace(checkout, control=build_control(checkout))
        checkout.control.load_config = lambda: load_config(args.config, cwd=repo)
        dispatch = pipeline(checkout)
        if args.verb == "retro":
            return asyncio.run(_retro(checkout, dispatch))
        if args.verb == "drain":
            from tempo.retro import Retro
            class DrainDispatch:
                _retro = Retro(checkout, dispatch)

                async def __call__(self, ticket):
                    return await dispatch(ticket)

                def __getattr__(self, name):
                    return getattr(dispatch, name)
            report = asyncio.run(drain.drain(checkout, DrainDispatch()))
            print(report.render(), end="")
            return report.exit_code
        return asyncio.run(runner.run_ticket(args.stem, checkout, dispatch))
    except (ConfigError, Refusal, LockHeld, IntakeRefused, JournalCorruption, ProviderSetupError,
            ExecutableNotFound, hostfiles.MarkerCorruption) as e:
        road = (" -- repair the selected config and retry retro"
                if args.verb == "retro" and isinstance(e, ConfigError) else "")
        print(f"tempo {args.verb}: {e}{road}", file=sys.stderr)
        return runner.EXIT_REFUSED


async def _retro(checkout, dispatch):
    from tempo.retro import Retro, RetroConstructionError, measurements, project_window, suppressing_failure

    lock = Lockfile(checkout.config.state_dir,
                    instance_id=await checkout.git.describe(checkout.repo), clock=checkout.clock)
    lock.acquire()
    try:
        try:
            window = project_window(checkout.journal.read())
        except JournalCorruption as exc:
            raise Refusal(str(exc), "restore journal readability from backup and retry retro") from None
        selected = bool(measurements(window)["merged_count"])
        suppressed = suppressing_failure(window) is not None
        if selected and not suppressed and hasattr(dispatch, "prepare"):
            await dispatch.prepare()
        try:
            result = await Retro(checkout, dispatch)(forced=True)
        except RetroConstructionError as exc:
            raise Refusal("retro Driver unavailable", str(exc)) from None
        if not selected:
            print("retro: no merge since the latest completed report")
            return 0
        if result is not None:
            print(f"retro: committed tickets/retro/{window.sequence:06d}.md")
            return 0
        print("retro: no report committed; inspect status and the Suggestion Box")
        return 1
    finally:
        try:
            await checkout.control.providers.close()
        finally:
            lock.release()


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


def _serve_signals(stop: Callable[[], None]) -> Callable[[], None]:
    """Real signal delivery is composed here, alongside the other operating-system seams."""
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        loop.add_signal_handler(sig, stop)
    def restore() -> None:
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
    return restore


async def _control(checkout: Checkout, verb: str) -> int:
    lock = Lockfile(checkout.config.state_dir, instance_id=await checkout.git.describe(checkout.repo),
                    clock=checkout.clock)
    try:
        lock.acquire()
    except LockHeld:
        try:
            lifecycle, hold = control.read_active(checkout.config.state_dir, Path.read_bytes)
        except ValueError as exc:
            raise Refusal("current control identity unavailable", str(exc)) from exc
        if verb == "resume" and hold is None:
            raise Refusal("no current hold identity",
                                 "read the running engine's current control identity and submit a new request")
        request = control.ControlRequest(uuid4().hex, lifecycle, verb, hold if verb == "resume" else None)
        control.publish_request(checkout.config.state_dir, request, checkout.fs)
        print(f"{verb} submitted: {request.request_id}")
    else:
        try:
            if verb == "kill":
                raise Refusal("nothing running to kill",
                                     "start a drain before submitting kill")
            print(f"nothing running to {verb}")
        finally:
            lock.release()
    return 0


async def _verdict(checkout, stem, verb):
    if findings := stem_findings(stem):
        raise Refusal(findings[0].message, findings[0].paved_road)
    try:
        return await runner.verdict(stem, checkout, kill=verb == "reject")
    except LockHeld:
        if verb != "confirm" or not runner.committed_draft(checkout, stem):
            raise
        text = await checkout.git._run(checkout.repo, "show", f"main:{ticket_path(stem)}")
        if validate_ticket(stem, text, checkout.repo).frontmatter.state != "draft":
            raise Refusal("no committed draft", "restore the committed draft before confirming")
    try:
        lifecycle, _ = control.read_active(checkout.config.state_dir, Path.read_bytes)
    except ValueError as exc:
        raise Refusal("current control identity unavailable", str(exc)) from exc
    request = control.AdmissionRequest(uuid4().hex, lifecycle, "confirm", stem, "operator")
    control.publish_request(checkout.config.state_dir, request, checkout.fs)
    print(f"{verb} submitted: {request.request_id}")
    return 0


def _new(repo: Path, stem: str) -> int:
    """Author the ticket file only: authorship is outside the lock fence; intake commits it on the next run."""
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
        print(f"fill these before `run {stem}`:")
        for f in e.findings:
            print(f"- {f.message} ({f.paved_road})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
