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
    Only cities and garages from enabled DLC states are included as location checks.
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
    death_link: DeathLink
