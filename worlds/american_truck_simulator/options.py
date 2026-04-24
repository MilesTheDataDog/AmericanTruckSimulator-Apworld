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
    Only cities, garages, and recruitment offices from enabled DLC states are included.
    Arizona is a free DLC and is safe to include for all players.
    California and Nevada (base game) are always included and do not need to be listed here.
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


class ShuffleTrucks(Toggle):
    """
    If enabled, truck models (other than the two base-game starting trucks) must be
    received as Archipelago items before they can be purchased at dealerships.
    The C++ plugin enforces this by hiding locked trucks in dealer menus.
    """
    display_name = "Shuffle Truck Models"
    default = 1


class ShuffleTruckUpgrades(Toggle):
    """
    If enabled, truck upgrade tiers (engine, transmission, cab, chassis, accessories)
    are shuffled into the item pool. Players start with only Tier 1 options available
    and must receive upgrade items to access better hardware.
    """
    display_name = "Shuffle Truck Upgrades"
    default = 0


class ShuffleGarages(Toggle):
    """
    If enabled, garages in each city must be received as Archipelago items before
    they can be purchased. Without the garage deed for a city, the purchase button
    is locked. Fully upgrading a received garage is still a location check.
    """
    display_name = "Shuffle Garages"
    default = 1


class ShuffleRecruitmentOffices(Toggle):
    """
    If enabled, recruitment offices are shuffled as items. Physically discovering
    an office (driving past it) is always a location check, but interacting with
    it to hire drivers requires receiving the corresponding office item.
    """
    display_name = "Shuffle Recruitment Offices"
    default = 1


class StartingState(Choice):
    """
    Which state the player starts in. California is always accessible regardless
    of this setting. Nevada (base game) is also always accessible.
    This controls which state the player's home garage begins in.
    """
    display_name = "Starting State"
    option_california = 0
    option_nevada = 1
    default = 0


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


class GarageUpgradeChecks(Toggle):
    """
    If enabled, fully upgrading a garage to 5 truck slots is a location check.
    Only garages in enabled DLC states are included.
    """
    display_name = "Garage Upgrade Checks"
    default = 1


class RecruitmentOfficeChecks(Toggle):
    """
    If enabled, discovering a recruitment office (driving past it for the first time)
    is a location check. Only offices in enabled DLC states are included.
    """
    display_name = "Recruitment Office Discovery Checks"
    default = 1


@dataclass
class ATSOptions(PerGameCommonOptions):
    win_condition: WinCondition
    goal_level: GoalLevel
    goal_money: GoalMoney
    enabled_dlc: EnabledDLC
    starting_state: StartingState
    shuffle_trucks: ShuffleTrucks
    shuffle_truck_upgrades: ShuffleTruckUpgrades
    shuffle_garages: ShuffleGarages
    shuffle_recruitment_offices: ShuffleRecruitmentOffices
    level_milestone_checks: LevelMilestoneChecks
    cargo_delivery_checks: CargoDeliveryChecks
    city_arrival_checks: CityArrivalChecks
    garage_upgrade_checks: GarageUpgradeChecks
    recruitment_office_checks: RecruitmentOfficeChecks
    death_link: DeathLink
