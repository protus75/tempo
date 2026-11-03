"""Identity-bound control requests."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Set
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from tempo.journal import Event, EventType, Journal
from tempo.seams import FileSystem, Sleep
from tempo.tickets import stem_findings

CONTROL_DECISION = "control_decision"
Verb = Literal["pause", "resume", "kill"]
_ID = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_SHAPE = ('submit a new request with exactly {request_id: nonempty ASCII letters/digits/_/-, '
          'lifecycle_id: nonempty string, verb: pause | resume | kill, hold_id: string | null}; '
          'match request_id to the filename; pause/kill require null hold_id; '
          'for resume read the current hold and submit its exact nonempty identity')
_ADMISSION_SHAPE = ('read the current committed draft and publish a fresh confirm request '
                    'with exactly {request_id, lifecycle_id, verb, stem, actor}; '
                    'verb is confirm and actor is operator | machine')
_ADMISSION_FIELDS = {"request_id", "lifecycle_id", "verb", "stem", "actor"}


@dataclass(frozen=True)
class ControlRequest:
    request_id: str
    lifecycle_id: str
    verb: Verb
    hold_id: str | None


@dataclass(frozen=True)
class AdmissionRequest:
    request_id: str
    lifecycle_id: str
    verb: Literal["confirm"]
    stem: str
    actor: Literal["operator", "machine"]


def validate_request(value: object, filename: str) -> ControlRequest | AdmissionRequest:
    if isinstance(value, dict) and value.get("verb") == "confirm":
        road = _ADMISSION_SHAPE
        if (set(value) != _ADMISSION_FIELDS
                or not isinstance(value.get("request_id"), str) or _ID.fullmatch(value["request_id"]) is None
                or filename != value["request_id"] + ".json"
                or not isinstance(value.get("lifecycle_id"), str) or not value["lifecycle_id"]
                or not isinstance(value.get("stem"), str) or stem_findings(value["stem"])
                or value.get("actor") not in ("operator", "machine")):
            raise ValueError(road)
        return AdmissionRequest(**value)
    if (not isinstance(value, dict)
            or set(value) != {"request_id", "lifecycle_id", "verb", "hold_id"}):
        raise ValueError(_SHAPE)
    request_id, lifecycle_id, verb, hold_id = (value[k] for k in
                                             ("request_id", "lifecycle_id", "verb", "hold_id"))
    if (not isinstance(request_id, str) or _ID.fullmatch(request_id) is None
            or filename != request_id + ".json"
            or not isinstance(lifecycle_id, str) or not lifecycle_id
            or not isinstance(verb, str) or verb not in {"pause", "resume", "kill"}
            or (verb == "resume" and (not isinstance(hold_id, str) or not hold_id))
            or (verb != "resume" and hold_id is not None)):
        raise ValueError(_SHAPE)
    return ControlRequest(request_id, lifecycle_id, verb, hold_id)


def publish_request(state_dir: Path, request: ControlRequest | AdmissionRequest, fs: FileSystem) -> None:
    value = asdict(request)
    validate_request(value, str(request.request_id) + ".json")
    try:
        fs.publish(state_dir / "control/inbox" / (request.request_id + ".json"),
                   json.dumps(value, separators=(",", ":")).encode())
    except FileExistsError as exc:
        raise FileExistsError("request id already exists; submit a new request id") from exc


def _object(pairs: list[tuple[str, object]]) -> dict:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError(_SHAPE)
    return value


@dataclass(frozen=True)
class ControlProjection:
    """Desired state, never an instruction to repeat an external action.

    Applications assign pause/kill state and release only the named hold identities.
    They must be idempotent across scans and reconstruction. Kill execution and its
    completion record belong to the kill path, not to this projection.
    """

    lifecycle_id: str
    pause_id: str | None = None
    released_hold_ids: frozenset[str] = frozenset()
    kill_requested: bool = False


def decisions(events: Iterable[Event]) -> Iterable[Event]:
    """Validate the sole inbox writer's evidence before granting release authority."""
    seen: dict[str, dict] = {}
    road = "repair the producing control evidence, never overwrite journal history"
    for event in events:
        body = event.body
        if body.get("kind") != CONTROL_DECISION:
            continue
        fields = {"kind", "request_id", "lifecycle_id", "verb", "hold_id", "decision", "reason"}
        if body.get("verb") == "confirm":
            fields = fields - {"hold_id"} | {"stem", "actor"}
        if (event.type != EventType.SIGNAL or event.ticket is not None or event.key is not None
                or set(body) != fields
                or not isinstance(body.get("request_id"), str)
                or _ID.fullmatch(body["request_id"]) is None
                or not isinstance(body.get("decision"), str)
                or body.get("decision") not in {"accepted", "stale", "rejected"}
                or not isinstance(body.get("reason"), str) or not body["reason"]):
            raise ValueError(f"invalid control decision; {road}")
        request_id = body["request_id"]
        if body["decision"] == "rejected":
            if any(body[field] is not None for field in ("lifecycle_id", "verb", "hold_id")):
                raise ValueError(f"invalid rejected control decision; {road}")
        else:
            try:
                validate_request({field: body[field] for field in
                                  fields - {"kind", "decision", "reason"}},
                                 request_id + ".json")
            except ValueError as exc:
                raise ValueError(f"invalid control decision; {road}") from exc
        if request_id in seen:
            if seen[request_id] != body:
                raise ValueError(f"conflicting control decision; {road}")
            continue
        seen[request_id] = body
        yield event


class ControlInbox:
    """Serial consumer of pause/resume/kill and draft-confirm requests, owned by the journal lock holder.

    Listing and reading are injected filesystem seams. The owner supplies a fresh,
    restart-unique lifecycle identity and live hold identities, never hold kinds.
    """

    def __init__(self, *, journal: Journal, lifecycle_id: str,
                 holds: Callable[[], Set[str]], apply: Callable[[ControlProjection], None],
                 files: Callable[[], Iterable[Path]], read: Callable[[Path], bytes]) -> None:
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise ValueError("supply the owner's nonempty restart-unique lifecycle identity")
        self.journal, self.lifecycle_id = journal, lifecycle_id
        self.holds, self.apply, self.files, self.read = holds, apply, files, read
        self._applied: ControlProjection | None = None
        self.draft: Callable[[str], bool] = lambda _: False

    def _fold(self) -> tuple[set[str], ControlProjection, bool]:
        decided = set()
        pause = None
        released = set()
        killed = False
        accepted = False
        for event in decisions(self.journal.read()):
            body = event.body
            request_id = body["request_id"]
            decided.add(request_id)
            if body["decision"] != "accepted" or body["lifecycle_id"] != self.lifecycle_id:
                continue
            if body["verb"] == "confirm":
                continue
            accepted = True
            if body["verb"] == "pause":
                pause = request_id
            elif body["verb"] == "resume":
                released.add(body["hold_id"])
                if pause == body["hold_id"]:
                    pause = None
            elif body["verb"] == "kill":
                killed = True
        return decided, ControlProjection(self.lifecycle_id, pause, frozenset(released), killed), accepted

    def recover(self) -> ControlProjection:
        _, projection, accepted = self._fold()
        if accepted and projection != self._applied:
            self.apply(projection)
            self._applied = projection
        return projection

    def consume(self) -> None:
        projection = self.recover()
        decided, _, _ = self._fold()
        for path in sorted(self.files(), key=lambda path: path.name):
            # Private temporaries and unsafe filenames cannot supply a journal identity.
            if path.suffix != ".json" or _ID.fullmatch(path.stem) is None:
                continue
            if path.stem in decided:
                continue
            body = {"kind": CONTROL_DECISION, "request_id": path.stem,
                    "lifecycle_id": None, "verb": None, "hold_id": None,
                    "decision": "rejected", "reason": _SHAPE}
            try:
                value = json.loads(self.read(path), object_pairs_hook=_object)
                if isinstance(value, dict) and (value.get("verb") == "confirm" or set(value) == _ADMISSION_FIELDS):
                    body["reason"] = _ADMISSION_SHAPE
                request = validate_request(value, path.name)
            except (ValueError, UnicodeError):
                pass
            else:
                if isinstance(request, AdmissionRequest):
                    del body["hold_id"]
                body.update(asdict(request))
                live_holds = set(self.holds()) - projection.released_hold_ids
                if projection.pause_id is not None:
                    live_holds.add(projection.pause_id)
                if request.lifecycle_id != self.lifecycle_id:
                    body.update(decision="stale", reason="read the current lifecycle and submit a new request")
                elif isinstance(request, AdmissionRequest):
                    matched = self.draft(request.stem)
                    body.update(decision="accepted" if matched else "stale",
                                reason="matching committed draft" if matched else
                                "read the current committed draft and publish a fresh confirm request")
                elif request.verb == "resume" and request.hold_id not in live_holds:
                    body.update(decision="stale", reason="read the current hold and submit a new request")
                else:
                    body.update(decision="accepted", reason="matching lifecycle and hold identities")
            self.journal.append(EventType.SIGNAL, body, ticket=None, key=None)
            decided.add(path.stem)
            if body["decision"] == "accepted":
                projection = self.recover()

    async def settle_admissions(self, draft_confirm) -> None:
        """Accepted decisions survive both crash boundaries; projection never grants authority."""
        for event in decisions(self.journal.read()):
            body = event.body
            if body["decision"] != "accepted" or body["verb"] != "confirm":
                continue
            key = f"ticket-plane/{body['stem']}/confirmed/{body['request_id']}"
            completed = [e.body["result"] for e in self.journal.read()
                         if e.type == EventType.EFFECT_COMPLETION and e.key == key]
            if not completed or not any(e.ticket == body["stem"] and e.body.get("signal") == "draft_confirmed"
                                       and e.body.get("commit") == completed[0]["commit"] for e in self.journal.read()):
                await draft_confirm(body["stem"], body["request_id"])


_DISCOVERY_ROAD = ("retry after the running engine publishes its current control identity; "
                   "read that identity and submit a new request")


def write_active(state_dir: Path, projection: ControlProjection | None, fs: FileSystem, *,
                 hold_id: str | None) -> None:
    value = (None if projection is None else
             {"lifecycle_id": projection.lifecycle_id, "hold_id": hold_id})
    fs.write(state_dir / "control/active.json", (json.dumps(value) + "\n").encode())


def read_active(state_dir: Path, read: Callable[[Path], bytes]) -> tuple[str, str | None]:
    try:
        value = json.loads(read(state_dir / "control/active.json"), object_pairs_hook=_object)
        if (not isinstance(value, dict) or set(value) != {"lifecycle_id", "hold_id"}
                or not isinstance(value["lifecycle_id"], str) or not value["lifecycle_id"]
                or (value["hold_id"] is not None and
                    (not isinstance(value["hold_id"], str) or not value["hold_id"]))):
            raise ValueError
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(_DISCOVERY_ROAD) from exc
    return value["lifecycle_id"], value["hold_id"]

KILL_APPLIED = "kill_applied"


def control_inbox(*, journal: Journal, lifecycle_id: str, holds: Callable[[], Set[str]],
                  apply: Callable[[ControlProjection], None],
                  files: Callable[[], Iterable[Path]], read: Callable[[Path], bytes]) -> ControlInbox:
    """Explicitly supplied by the lock holder, never a second writer."""
    return ControlInbox(journal=journal, lifecycle_id=lifecycle_id, holds=holds,
                        apply=apply, files=files, read=read)


async def executor_abort(abort_current: Callable[[], Awaitable[None]]) -> None:
    """Unwind the composed executor after durable kill acceptance."""
    await abort_current()


async def protected_cleanup(cleanup: asyncio.Future) -> None:
    cancelled = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    cleanup.result()
    if cancelled is not None:
        raise cancelled


class PauseConsumer:
    """One serial inbox and desired pause state, owned by the engine's writer lock."""

    def __init__(self, *, journal: Journal, lifecycle_id: str, state_dir: Path,
                 fs: FileSystem, sleep: Sleep, files: Callable[[], Iterable[Path]],
                 read: Callable[[Path], bytes], recover: Callable[[], None] | None = None,
                 storm_holds: Callable[[], dict[str, str]] | None = None) -> None:
        self._recover = recover
        self._storm_holds = storm_holds
        self.state_dir, self.fs, self.sleep = state_dir, fs, sleep
        self.projection = ControlProjection(lifecycle_id)
        self._published = False
        self._advertised: tuple[str, str | None] | None = None
        self._stop: asyncio.Task | None = None
        self.admission: str | None = None
        self.inbox = control_inbox(journal=journal, lifecycle_id=lifecycle_id,
                                   holds=self._holds,
                                   apply=self._apply, files=files, read=read)

    def storm_holds(self) -> dict[str, str]:
        return {} if self._storm_holds is None else self._storm_holds()

    def _holds(self) -> set[str]:
        return set(self.storm_holds()) | ({self.admission} if self.admission is not None else set())

    def _apply(self, projection: ControlProjection) -> None:
        # The inbox has already fsynced the decision; discovery is only its projection.
        if self._published:
            self._refresh(projection)
        self.projection = projection

    def _selected_hold(self, projection: ControlProjection) -> str | None:
        if projection.pause_id is not None:
            return projection.pause_id
        if self.admission is not None and self.admission not in projection.released_hold_ids:
            return self.admission
        return next((identity for identity in self.storm_holds()
                     if identity not in projection.released_hold_ids), None)

    def _refresh(self, projection: ControlProjection) -> None:
        hold = self._selected_hold(projection)
        advertised = projection.lifecycle_id, hold
        if advertised != self._advertised:
            write_active(self.state_dir, projection, self.fs, hold_id=hold)
            self._advertised = advertised

    def hold(self, hold_id: str) -> None:
        self.admission = hold_id
        if self._published:
            self._refresh(self.projection)

    def publish(self) -> None:
        self._refresh(self.projection)
        self._published = True

    def retire(self) -> None:
        write_active(self.state_dir, None, self.fs, hold_id=None)
        self._published = False
        self._advertised = None

    async def checkpoint(self) -> None:
        while True:
            self.poll()
            if self.projection.kill_requested or self.projection.pause_id is None:
                return
            await self.sleep(0.1)

    def poll(self) -> None:
        """Consume control without occupying a paused offer's callback."""
        if self._recover is not None:
            self._recover()
        if self._published:
            self._refresh(self.projection)
        self.inbox.consume()

    async def apply_kill(self, abort: Callable[[], Awaitable[None]],
                         dispatch: asyncio.Task | None = None) -> None:
        if (self.projection.lifecycle_id != self.inbox.lifecycle_id
                or not self.projection.kill_requested):
            return
        if self._stop is None:
            async def stop() -> None:
                await executor_abort(abort)
                if dispatch is not None:
                    if not dispatch.done() and not dispatch.cancelling():
                        dispatch.cancel()
                    [result] = await asyncio.gather(dispatch, return_exceptions=True)
                    if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                        raise result
            self._stop = asyncio.create_task(stop())
        await protected_cleanup(self._stop)
        # Requests arriving during unwind share that same stop. The journal fold,
        # rather than file deletion or memory, owns completion across reconstruction.
        self.inbox.consume()
        decided, accepted, applied = set(), [], set()
        for event in self.inbox.journal.read():
            body = event.body
            if event.type != EventType.SIGNAL:
                continue
            if (body.get("kind") == KILL_APPLIED
                    and body.get("lifecycle_id") == self.inbox.lifecycle_id):
                applied.add(body["request_id"])
            if body.get("kind") != CONTROL_DECISION or body["request_id"] in decided:
                continue
            decided.add(body["request_id"])
            if (body["decision"] == "accepted" and body["verb"] == "kill"
                    and body["lifecycle_id"] == self.inbox.lifecycle_id):
                accepted.append(body["request_id"])
        for request_id in accepted:
            if request_id not in applied:
                self.inbox.journal.append(EventType.SIGNAL, {
                    "kind": KILL_APPLIED, "request_id": request_id,
                    "lifecycle_id": self.inbox.lifecycle_id}, ticket=None, key=None)
