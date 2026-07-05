from dataclasses import dataclass
from Options import Choice, Range, Toggle, OptionSet, PerGameCommonOptions, DeathLink


class WinCondition(Choice):
    """
    How to win American Truck Simulator.

    level_and_money: Reach the goal level AND the goal money amount (both required).
    level_only:      Reach the goal level.
    money_only:      Reach the goal money amount.
    level_or_money:  Reach either the goal level OR the goal money amount (whichever comes first).
    """
    display_name = "Win Condition"
    option_level_and_money = 0
    option_level_only = 1
    option_money_only = 2
    option_level_or_money = 3
    default = 0
    # Explicit aliases ensure the names are recognised even in AP builds where
    # the auto-generated name_lookup from option_* attributes is unreliable.
    # Numeric-string aliases ("0", "1", ...) handle the case where AP receives
    # an integer from YAML, converts it to str, then calls from_text().
    aliases = {
        "level_and_money": 0,
        "level_only":      1,
        "money_only":      2,
        "level_or_money":  3,
        "0": 0,
        "1": 1,
        "2": 2,
        "3": 3,
    }


class GoalLevel(Range):
    """
    Target driver level to reach for the level win condition.
    Level 35 is the maximum needed to unlock all driver skill upgrades.
    """
    display_name = "Goal Level"
    range_start = 5
    range_end = 35
    default = 35


class GoalMoney(Range):
    """
    Target money in thousands of dollars for the money win condition.
    Default 1000 = $1,000,000. Maximum 10000 = $10,000,000.
    """
    display_name = "Goal Money (thousands $)"
    range_start = 100
    range_end = 10000
    default = 1000


class EnabledDLC(OptionSet):
    """
    Which DLC map packs you own and want included in the randomizer.
    Only cities in enabled DLC states are included as location checks.
    California and Nevada (base game) are always included.
    Arizona is a free DLC and is safe to include for all players.
    """
    display_name = "Enabled DLC States"
    valid_keys = {
        "Arizona",
        "New Mexico",
        "Oregon",
        "Washington",
        "Utah",
        "Idaho",
        "Colorado",
        "Wyoming",
        "Montana",
        "Texas",
        "Oklahoma",
        "Kansas",
        "Nebraska",
        "Arkansas",
        "Missouri",
        "Iowa",
        "Louisiana",
    }
    default = {"Arizona"}


class LevelMilestoneChecks(Toggle):
    """
    If enabled, reaching driver levels 5, 10, 15, 20, 25, and 30 each count as
    a location check that sends an item to the multiworld pool.
    """
    display_name = "Level Milestone Checks"
    default = 1


class CargoDeliveryChecks(Toggle):
    """
    If enabled, completing the first delivery of each vanilla cargo type is a
    location check. There are approximately 128 cargo types in the base game.
    """
    display_name = "Cargo Delivery Checks"
    default = 1


class CityArrivalChecks(Toggle):
    """
    If enabled, arriving in a city for the first time is a location check.
    Only cities in enabled DLC states are included.
    """
    display_name = "City First Arrival Checks"
    default = 1


class StateArrivalChecks(Toggle):
    """
    If enabled, entering a DLC state for the first time is a location check.
    One check fires per enabled DLC state the first time you arrive in any city
    within it. California and Nevada are excluded as they are always accessible.
    """
    display_name = "State First Visit Checks"
    default = 1


class StateUnlocks(Toggle):
    """
    If enabled, each DLC state must be unlocked by receiving its "Unlock <State>"
    item from the multiworld before that state's city and first-visit checks can
    be sent. You can still physically drive anywhere and deliver anywhere at any
    time — nothing in the game is blocked. The only effect is that arrival checks
    in a locked state are held by the client and released the moment its unlock
    item arrives, turning your states into real Archipelago progression.

    California and Nevada (base game) are always unlocked. This option has no
    effect unless City First Arrival Checks and/or State First Visit Checks are
    also enabled (those are the checks it gates).
    """
    display_name = "State Unlock Progression"
    default = 0


class TrapPercentage(Range):
    """
    Percentage of filler item slots that will be replaced with money trap items
    (fines). When a trap is received, the fine amount is deducted from the
    player's in-game money. The fine cannot reduce the balance below $0.

    Fine amounts: $500, $1,000, $2,500, $5,000, $10,000, $25,000, $50,000.
    """
    display_name = "Trap Percentage"
    range_start = 0
    range_end = 100
    default = 0


class DeathLinkPenalty(Range):
    """
    Percentage of your current in-game money deducted when a death link is
    received from another player (an "emergency towing fee"). Only used when
    death_link is enabled. The balance cannot drop below $0.

    Your own truck sends a death to other players when its engine damage
    reaches 90% (effectively undriveable).
    """
    display_name = "Death Link Penalty (%)"
    range_start = 0
    range_end = 50
    default = 15


@dataclass
class ATSOptions(PerGameCommonOptions):
    win_condition: WinCondition
    goal_level: GoalLevel
    goal_money: GoalMoney
    enabled_dlc: EnabledDLC
    level_milestone_checks: LevelMilestoneChecks
    cargo_delivery_checks: CargoDeliveryChecks
    city_arrival_checks: CityArrivalChecks
    state_arrival_checks: StateArrivalChecks
    state_unlocks: StateUnlocks
    trap_percentage: TrapPercentage
    death_link: DeathLink
    death_link_penalty: DeathLinkPenalty
