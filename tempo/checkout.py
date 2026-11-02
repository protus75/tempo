"""The composed seams one invocation runs against, and the engine-plane refusal."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tempo.config import Config, ConfigSnapshot
from tempo.git import Git
from tempo.journal import Journal
from tempo.seams import Clock, FileSystem, GroupExec, Sleep

if TYPE_CHECKING:
    from tempo.control import PauseConsumer


# Engine plane: the prompt specs ship with the engine, not the host checkout it runs against.
SPECS_DIR = Path(__file__).resolve().parent.parent / "specs"


# The provider backstop is the drain ceiling every ticket stuck budget fits under (section 9.7):
# the ticket's own stuck budget is the bound the driver enforces, never a shorter per-call cap.
def call_timeout(config: Config) -> float:
    return config.drain.max_ticket_minutes * 60.0


class Refusal(Exception):
    """An engine-plane refusal (exit 2): nothing was dispatched."""

    def __init__(self, message: str, paved_road: str) -> None:
        super().__init__(f"{message} -- {paved_road}")


@dataclass(frozen=True)
class Checkout:
    """The composed seams one invocation runs against; the stage pipeline is built from it."""

    repo: Path
    config: Config | ConfigSnapshot
    env: Mapping[str, str]
    exec_: GroupExec
    git: Git
    journal: Journal
    fs: FileSystem
    clock: Clock
    sleep: Sleep = asyncio.sleep
    control: "PauseConsumer | None" = None
