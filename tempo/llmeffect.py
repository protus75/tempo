"""The LLM call as an effect (TEMPO_PLAN.md sections 2 and 6).

Keyed `llm/<stem>/<run_seq>/<surface>/<attempt>/<call_seq>`: one completion per call naming its served route,
and a same-key re-entry (no intervening terminal, so the same run_seq) replays the recorded result
instead of calling. The driver reaches the model only through `llm_call`.
"""

from dataclasses import asdict, replace
from typing import Any

from tempo.effects import Effects
from tempo.llm import LLM, LLMRequest
from tempo.redact import Redactor


def llm_key(stem: str, run_seq: int, surface: str, attempt: int, call_seq: int) -> str:
    return "/".join(("llm", stem, str(run_seq), surface, str(attempt), str(call_seq)))


def _served(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in ("provider", "model")}


async def llm_call(
    effects: Effects, llm: LLM, req: LLMRequest, redactor: Redactor, *, ticket: str | None,
    stem: str, run_seq: int, attempt: int, call_seq: int
) -> dict[str, Any]:
    async def action():
        result = await llm.call(req)
        return asdict(replace(result, text=redactor.scrub(result.text)))
    key = llm_key(stem, run_seq, req.surface, attempt, call_seq)
    return await effects.run(action, key=key, ticket=ticket, served=_served)
