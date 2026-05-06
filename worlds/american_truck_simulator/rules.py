from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ATSWorld


def set_rules(world: "ATSWorld") -> None:
    """
    Set access rules for locations.

    All state regions are always accessible — no unlock items gate entry to
    any state or city. The client enforces truck/upgrade restrictions by
    filtering dealer menus; no Archipelago access rules are needed for those.
    """
    # No location rules are currently needed.
    # The goal location's win condition is handled by generate_basic().
    pass
