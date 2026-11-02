"""Starting-state policy for box-authored tickets (plan section 12)."""

from collections.abc import Sequence

from tempo.box import MESSAGE_CLASSES
from tempo.config import BoxRow, Config, StartState


def policy_row(message_class: str, bug_origin: str | None, has_repro: bool | None) -> BoxRow:
    """Map a box message's class-specific fields to its policy-table row."""
    if message_class == "bug_report":
        if bug_origin == "self_diagnosed" and has_repro is not None:
            return "bug_report_self_diagnosed"
        if bug_origin == "player" and has_repro is True:
            return "bug_report_player_repro"
        if bug_origin == "player" and has_repro is False:
            return "bug_report_player_no_repro"
        raise ValueError("bug_report requires bug_origin and has_repro")
    if message_class in MESSAGE_CLASSES:
        return message_class
    raise ValueError(f"unknown message class {message_class!r}")


def _path_overlap(left: str, right: str) -> bool:
    left, right = left.rstrip("/"), right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def touches_inventory(fence: Sequence[str], inventory: Sequence[str]) -> bool:
    """Whether any scope-fence and safety-inventory paths overlap at a boundary."""
    return any(_path_overlap(fenced, protected) for fenced in fence for protected in inventory)


def start_state(
    config: Config,
    *,
    row: BoxRow,
    fence: Sequence[str],
    gate_bypass: Sequence[str],
    reopen: bool = False,
) -> StartState:
    """Resolve a box ticket's initial state, failing closed for every override."""
    if (reopen or not config.engine_plane_safety_inventory
            or touches_inventory(fence, config.engine_plane_safety_inventory) or gate_bypass):
        return "draft"
    return config.box_policy[row]
