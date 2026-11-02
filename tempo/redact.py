"""Redaction seam (TEMPO_PLAN.md section 6): one shared filter every captured-stream writer is built with.

Writer-site checklist (extend in the same change that adds a writer of a captured stream):
- attempt-spool writer: tempo/driver.py `Spool`
- engine-log writer: tempo/enginelog.py `EngineLog`
- attempt-spool writer: tempo/call.py `Calls._complete` (the subagent's reply)
- journal writer: tempo/journal.py -- NOT YET WIRED; its one secret-bearing producer, the LLM effect
  result, is scrubbed before it becomes the completion record (tempo/llmeffect.py `llm_call`)
"""

from collections.abc import Mapping


class Redactor:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        # An empty value would match everywhere; longest first so an embedded shorter secret cannot split a longer one.
        self._secrets = sorted(((n, v) for n, v in secrets.items() if v), key=lambda nv: -len(nv[1]))

    def scrub(self, text: str) -> str:
        for name, value in self._secrets:
            text = text.replace(value, f"[REDACTED:{name}]")
        return text
