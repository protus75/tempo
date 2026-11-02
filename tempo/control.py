"""Identity-bound control requests (TEMPO_PLAN.md 19.P3.control-inbox)."""

import json
import re
from collections.abc import Callable, Iterable, Set
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from tempo.journal import Event, EventType, Journal
from tempo.seams import FileSystem
from tempo.effects import Effects
from tempo.tickets import stem_findings

CONTROL_DECISION = "control_decision"
MERGE_RELEASE = "merge-release"
Verb = Literal["pause", "resume", "kill"]
_ID = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_SHAPE = ('submit a new request with exactly {request_id: nonempty ASCII letters/digits/_/-, '
          'lifecycle_id: nonempty string, verb: pause | resume | kill, hold_id: string | null}; '
          'match request_id to the filename; pause/kill require null hold_id; '
          'for resume read the current hold and submit its exact nonempty identity')
_ADMISSION_SHAPE = ('read the current admission and publish a fresh matching confirm | reject request '
                    'with exactly {request_id, lifecycle_id, verb, stem, hold_id, actor}; '
                    'actor is operator | machine and only a committed draft confirm has null hold_id')


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
    verb: Literal["confirm", "reject"]
    stem: str
    hold_id: str | None
    actor: Literal["operator", "machine"]


def validate_request(value: object, filename: str) -> ControlRequest | AdmissionRequest:
    if isinstance(value, dict) and value.get("verb") in ("confirm", "reject"):
        road = _ADMISSION_SHAPE
        if (set(value) != {"request_id", "lifecycle_id", "verb", "stem", "hold_id", "actor"}
                or not isinstance(value.get("request_id"), str) or _ID.fullmatch(value["request_id"]) is None
                or filename != value["request_id"] + ".json"
                or not isinstance(value.get("lifecycle_id"), str) or not value["lifecycle_id"]
                or not isinstance(value.get("stem"), str) or stem_findings(value["stem"])
                or value.get("actor") not in ("operator", "machine")
                or (value.get("hold_id") is not None and
                    (not isinstance(value["hold_id"], str) or not value["hold_id"]))
                or value["verb"] == "reject" and value["hold_id"] is None):
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
    completion record belong to the later activation, not to this projection.
    """

    lifecycle_id: str
    pause_id: str | None = None
    released_hold_ids: frozenset[str] = frozenset()
    kill_requested: bool = False


def decisions(events: Iterable[Event]) -> Iterable[Event]:
    """Validate the sole inbox writer's evidence before granting release authority."""
    seen: dict[str, dict] = {}
    verdicts = {}
    road = "repair the producing control evidence, never overwrite journal history"
    for event in events:
        body = event.body
        if body.get("kind") != CONTROL_DECISION:
            continue
        fields = {"kind", "request_id", "lifecycle_id", "verb", "hold_id", "decision", "reason"}
        admission = body.get("verb") in ("confirm", "reject")
        if admission:
            fields |= {"stem", "actor"}
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
        if admission and body["decision"] == "accepted" and body["hold_id"] is not None:
            if body["hold_id"] in verdicts:
                raise ValueError(f"conflicting admission verdict; {road}")
            verdicts[body["hold_id"]] = request_id
        yield event


class ControlInbox:
    """Serial consumer owned by the existing journal lock holder.

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
        self.admissions: Callable[[], dict[str, str]] = lambda: {}
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
            if body["verb"] in ("confirm", "reject"):
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
                if isinstance(value, dict) and (value.get("verb") in ("confirm", "reject")
                        or set(value) == {"request_id", "lifecycle_id", "verb", "stem", "hold_id", "actor"}):
                    body["reason"] = _ADMISSION_SHAPE
                request = validate_request(value, path.name)
            except (ValueError, UnicodeError):
                pass
            else:
                body.update(asdict(request))
                live_holds = set(self.holds()) - projection.released_hold_ids
                if projection.pause_id is not None:
                    live_holds.add(projection.pause_id)
                if request.lifecycle_id != self.lifecycle_id:
                    body.update(decision="stale", reason="read the current lifecycle and submit a new request")
                elif isinstance(request, AdmissionRequest):
                    prior = [e.body for e in decisions(self.journal.read())
                             if e.body["decision"] == "accepted" and e.body.get("verb") in ("confirm", "reject")]
                    matched = (self.admissions().get(request.stem) == request.hold_id
                               if request.hold_id is not None else self.draft(request.stem))
                    duplicate = request.hold_id is not None and any(
                        b["hold_id"] == request.hold_id and b["stem"] == request.stem for b in prior)
                    body.update(decision="accepted" if matched and not duplicate else "stale",
                                reason="matching admission" if matched and not duplicate else
                                "read the current admission and publish a fresh matching request")
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
            if body["decision"] != "accepted" or body["verb"] not in ("confirm", "reject"):
                continue
            if body["hold_id"] is None:
                key = f"ticket-plane/{body['stem']}/confirmed/{body['request_id']}"
                completed = [e.body["result"] for e in self.journal.read()
                             if e.type == EventType.EFFECT_COMPLETION and e.key == key]
                if not completed or not any(e.ticket == body["stem"] and e.body.get("signal") == "draft_confirmed"
                                           and e.body.get("commit") == completed[0]["commit"] for e in self.journal.read()):
                    await draft_confirm(body["stem"], body["request_id"])
                continue
            hold = body["hold_id"]
            result = {k: body[k] for k in ("stem", "hold_id", "request_id", "lifecycle_id", "actor")}
            result.update(attempt=int(hold.split("/")[2]), verdict=body["verb"])
            async def complete(result=result):
                return result
            await Effects(self.journal).run(complete,
                key=f"{MERGE_RELEASE}/{hold}/{body['request_id']}", ticket=body["stem"])


def releases(events: Iterable[Event]) -> dict[str, dict]:
    from tempo.mergequeue import regate_receipts
    events = tuple(events)
    receipts = regate_receipts(events)
    accepted = {e.body["request_id"]: e.body for e in decisions(events)
                if e.body["decision"] == "accepted" and e.body["verb"] in ("confirm", "reject")}
    result = {}
    road = "restore matching control decision and release evidence before retrying admission"
    for event in events:
        if event.type != EventType.EFFECT_COMPLETION or not (event.key or "").startswith(MERGE_RELEASE + "/"):
            continue
        value = event.body.get("result")
        if not isinstance(value, dict) or set(value) != {"stem", "attempt", "hold_id", "request_id", "lifecycle_id", "verdict", "actor"}:
            raise ValueError(road)
        decision = accepted.get(value["request_id"])
        hold = value["hold_id"]
        if (decision is None or hold is None or not isinstance(hold, str) or hold not in receipts
                or receipts[hold]["admission"] != "HELD"
                or event.ticket != value["stem"] or event.key != f"{MERGE_RELEASE}/{hold}/{value['request_id']}"
                or any(value[k] != decision[k] for k in ("stem", "hold_id", "request_id", "lifecycle_id", "actor"))
                or value["verdict"] != decision["verb"] or type(value["attempt"]) is not int
                or not re.fullmatch(r"merge-regate/[^/]+/[0-9]+/[0-9a-f]{40}/[0-9a-f]{40}", hold)
                or value["attempt"] != int(hold.split("/")[2]) or value["stem"] != hold.split("/")[1]
                or hold in result and result[hold] != value):
            raise ValueError(road)
        result[hold] = value
    return result


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
