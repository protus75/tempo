"""Redaction seam (TEMPO_PLAN.md section 6): one shared filter every captured-stream writer is built with.

Writer-site checklist (extend in the same change that adds a writer of a captured stream):
- attempt-spool writer: tempo/driver.py `Spool`
- engine-log writer: tempo/enginelog.py `EngineLog`
- attempt-spool writer: tempo/providers.py `CliAdapter.invoke` (prompt, cli event stream, stderr)
- journal writer: tempo/journal.py -- NOT YET WIRED; its one secret-bearing producer, the LLM effect
  result, is scrubbed before it becomes the completion record (tempo/llmeffect.py `llm_call`)
"""

from collections.abc import Mapping

from tempo.config import Config, ConfigSnapshot


class Redactor:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        # An empty value would match everywhere; longest first so an embedded shorter secret cannot split a longer one.
        self._secrets = sorted(((n, v) for n, v in secrets.items() if v), key=lambda nv: -len(nv[1]))

    @classmethod
    def from_config(cls, config: Config | ConfigSnapshot, env: Mapping[str, str]) -> "Redactor":
        """Resolve each provider's `auth` env-var NAME to its value; unset names have nothing to leak."""
        return cls({p.auth: env[p.auth] for p in config.providers if p.auth and p.auth in env})

    def scrub(self, text: str) -> str:
        for name, value in self._secrets:
            text = text.replace(value, f"[REDACTED:{name}]")
        return text
