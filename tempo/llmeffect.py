"""The LLM call as an effect (TEMPO_PLAN.md sections 2 and 6).

Keyed `llm/<stem>/<run_seq>/<surface>/<attempt>/<call_seq>`: one cost-bearing completion per call,
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


def _cost(result: dict[str, Any]) -> dict[str, Any]:
    return {k: result[k] for k in ("usd", "input_tokens", "output_tokens", "provider", "model")}


async def llm_call(
    effects: Effects, llm: LLM, req: LLMRequest, redactor: Redactor, *, ticket: str | None,
    stem: str, run_seq: int, attempt: int, call_seq: int
) -> dict[str, Any]:
    key = llm_key(stem, run_seq, req.surface, attempt, call_seq)
    async def record(operation):
        async def action():
            result = await operation()
            return asdict(replace(result, text=redactor.scrub(result.text)))
        return await effects.run(action, key=key, ticket=ticket, cost=_cost)
    # A replay has no admission, wait, meter, outcome or cooldown side effect.
    if key in effects._completed or not hasattr(llm, 'admitted_call'):
        return await record(lambda: llm.call(req))
    return await llm.admitted_call(req, ticket=ticket, call_key=key, effect=record)
