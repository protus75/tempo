"""The capability vocabulary every model call names (TEMPO_PLAN.md section 6)."""

from typing import Literal

AgentTier = Literal["low", "medium", "high", "max"]
AgentEffort = Literal["low", "medium", "high", "max"]
