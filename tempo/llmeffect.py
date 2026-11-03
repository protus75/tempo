"""The LLM call's effect key (TEMPO_PLAN.md sections 2 and 6).

Keyed `llm/<stem>/<run_seq>/<surface>/<attempt>/<call_seq>`: one completion per call naming its served route.
`tempo.call` begins the effect in `prepare` and completes it in `submit`.
"""


def llm_key(stem: str, run_seq: int, surface: str, attempt: int, call_seq: int) -> str:
    return "/".join(("llm", stem, str(run_seq), surface, str(attempt), str(call_seq)))
