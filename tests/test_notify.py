import json
import sys
from types import SimpleNamespace

import pytest

from tempo import __main__ as cli
from tempo.config import load_config
from tempo.effects import Effects
from tempo.enginelog import EngineLog
from tempo.journal import EventType, Journal
from tempo.notify import NOTIFY_RETRY_S, NotificationReconciler, notification_key, send
from tempo.redact import Redactor
from tempo.seams import CommandNotifications, NotificationFailed, SubprocessExec
from tests.test_config import VALID
from tests.test_scheduler import Time
from tests.test_scheduler import turn


@pytest.mark.asyncio
async def test_notify_effect_once_and_conservative_resend(tmp_path, monkeypatch):
    journal = Journal(tmp_path, Time())
    calls = []
    class Delivery:
        fail = False
        async def notify(self, argv):
            assert journal.read()[-1].type == EventType.EFFECT_INTENT
            calls.append(argv)
            if self.fail:
                raise NotificationFailed("repair command and retry")
            return {"rc": 0, "stdout": "delivered", "stderr": ""}
    seam = Delivery()
    async def deliver(effects, identity="event"):
        return await send(effects, seam, ["notify", "fixed argument"], owner="stem",
            escalation="ordinary", identity=identity, message="resume", ticket="stem")
    effects = Effects(journal)
    result = await deliver(effects)
    assert await deliver(effects) == await deliver(Effects(journal)) == result
    assert len(calls) == 1 and calls[0] == ["notify", "fixed argument", "resume"]
    append = journal.append
    def crash(type, *args, **kwargs):
        if type == EventType.EFFECT_COMPLETION:
            raise OSError("crash after external send")
        return append(type, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(journal, "append", crash)
        with pytest.raises(OSError, match="crash"):
            await deliver(effects, "orphan")
    assert len(calls) == 2
    await deliver(Effects(journal), "orphan")
    assert len(calls) == 3
    seam.fail = True
    with pytest.raises(NotificationFailed):
        await deliver(Effects(journal), "failed")
    assert not any(e.type == EventType.EFFECT_COMPLETION and e.key.endswith("/failed")
                   for e in journal.read())
    seam.fail = False
    await deliver(Effects(journal), "failed")
    assert len(calls) == 5
    assert [e.type for e in journal.read() if e.key.endswith("/failed")] == [
        EventType.EFFECT_INTENT, EventType.EFFECT_INTENT, EventType.EFFECT_COMPLETION]


def test_notify_key_domains():
    for kind in ("ordinary", "stuck-past-threshold"):
        assert notification_key("stem", kind, "identity") == f"notify/stem/{kind}/identity"


@pytest.mark.asyncio
async def test_notify_seam_uses_argv_and_secret_free_env(tmp_path):
    secret = 'configured-secret-"\\value'
    (tmp_path / "config.yaml").write_text(VALID.replace("kind: cli", "kind: cli\n    auth: TEST_KEY"))
    config = load_config(None, cwd=tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TEST_KEY": secret, "KEEP": "ambient"}
    clock = Time()
    journal = Journal(tmp_path / "state", clock)
    work = SubprocessExec()
    checkout = SimpleNamespace(config=config, repo=tmp_path, env=env, exec_=work, journal=journal, clock=clock)
    log = EngineLog(tmp_path / "engine.log", Redactor.from_config(config, env), clock)
    reconciler = cli.build_notifications(checkout, config_path=None, log=log)
    seam = reconciler.compose(config)
    assert isinstance(seam, CommandNotifications)
    assert seam.exec_ is not work
    assert seam.cwd == tmp_path and seam.timeout == 30
    script = ("import os,sys,json; from pathlib import Path; "
              "print(json.dumps([sys.argv[1:],str(Path.cwd()),dict(os.environ)])); "
              f"print({secret!r}); print({secret!r},file=sys.stderr)")
    argv = [sys.executable, "-c", script, "spaces;$(literal)"]
    effects = Effects(journal)
    result = await effects.run(lambda: seam.notify(argv), key="notify/test/argv/1", ticket=None)
    payload = json.loads(result["stdout"].splitlines()[0])
    assert payload[0] == ["spaces;$(literal)"] and payload[1] == str(tmp_path)
    assert "TEST_KEY" not in payload[2] and payload[2]["KEEP"] == "ambient"
    assert secret not in result["stdout"] + result["stderr"]
    assert "[REDACTED:TEST_KEY]" in result["stdout"] and "[REDACTED:TEST_KEY]" in result["stderr"]
    assert await Effects(journal).run(lambda: seam.notify(argv), key="notify/test/argv/1", ticket=None) == result
    for command in (["tempo-missing-notification-binary"],
                    [sys.executable, "-c", f"import sys; print({secret!r}); sys.exit(4)"]):
        with pytest.raises(NotificationFailed) as exc:
            await effects.run(lambda: seam.notify(command), key=f"notify/test/failure/{command[0]}", ticket=None)
        assert secret not in str(exc.value) and "retry" in str(exc.value)
        log.event("notify_failed", error=str(exc.value))
    assert secret not in json.dumps([e.body for e in journal.read()]) + log.path.read_text()
    for malformed in ([], "notify", [""], ["notify", 3], ["notify", "\0"]):
        with pytest.raises(ValueError, match="argv"):
            await seam.notify(malformed)


@pytest.mark.asyncio
async def test_notify_timeout_backoff_and_unchanged_poll_cache(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(VALID + "notify: [notify]\n")
    config = load_config(None, cwd=tmp_path)
    clock = Time()
    journal = Journal(tmp_path / "state", clock)
    journal.append(EventType.SIGNAL, {"kind": "merge_tree_mismatch", "hold_id": "hold",
        "checked_tree": "checked", "main_tree": "main"}, ticket="stem")
    loads, reads, calls = [], [], []
    read = journal.read
    def reading():
        reads.append(None)
        return read()
    monkeypatch.setattr(journal, "read", reading)
    def load():
        loads.append(None)
        return config
    class TimeoutExec:
        async def run(self, argv, **kwargs):
            calls.append(argv)
            raise TimeoutError("scripted timeout")
    seam = CommandNotifications(TimeoutExec(), cwd=tmp_path, env={}, timeout=30, scrub=lambda s: s)
    log = EngineLog(tmp_path / "engine.log", Redactor.from_config(config, {}), clock)
    reconciler = NotificationReconciler(journal=journal, clock=clock, config_path=tmp_path / "config.yaml",
        load=load, compose=lambda _: seam, log=log)
    try:
        reconciler.poll(startup=True)
        await turn()
        reconciler.poll()
        snapshot = len(reads), len(loads)
        for _ in range(100):
            reconciler.poll()
            clock.advance(.1)
            await turn()
        assert (len(reads), len(loads)) == snapshot and len(calls) == 1
        assert len(reconciler.pending) == 1 and not any(
            e.type == EventType.EFFECT_COMPLETION for e in read())
        clock.advance(NOTIFY_RETRY_S)
        reconciler.poll()
        await turn()
        assert len(calls) == 2
        assert "retry pending evidence" in log.path.read_text()
    finally:
        await reconciler.close()
