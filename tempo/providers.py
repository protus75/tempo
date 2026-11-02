"""Provider failure vocabulary (TEMPO_PLAN.md section 6).

Tempo runs no agent CLI (AGENT_PLAN.md R15): every model call is a host subagent (tempo/call.py),
whose stop note is classified by these markers.
"""

from typing import Literal

# Closed allowlist of tree-writing surfaces: every other surface is called read-only.
WRITING_SURFACES = frozenset({"implement"})

FailureClass = Literal["rate_limited", "quota_exhausted", "outage", "auth_error", "model_error", "unclassified"]

FAILURE_MARKERS: tuple[tuple[FailureClass, tuple[str, ...]], ...] = (
    ("quota_exhausted", ("quota exhausted", "usage limit reached", "out of extra usage")),
    ("rate_limited", ("rate limit", "too many requests")),
    ("outage", ("service unavailable", "internal server error", "stream disconnected", "at capacity")),
    ("model_error", ("model unavailable", "model not found", "model is not supported")),
)


class ProviderCallError(Exception):
    """A model call failed with a classified failure. `stderr_tail` is scrubbed."""

    def __init__(
        self,
        provider: str,
        message: str,
        *,
        rc: int,
        stderr_tail: str,
        failure_class: str | None = None,
        paved_road: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {message} (exit {rc}); stderr tail: {stderr_tail!r}")
        self.provider = provider
        self.rc = rc
        self.stderr_tail = stderr_tail
        self.failure_class = failure_class
        self.paved_road = paved_road
