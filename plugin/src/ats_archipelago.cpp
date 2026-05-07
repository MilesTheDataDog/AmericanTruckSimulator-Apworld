/**
 * ats_archipelago.cpp
 *
 * American Truck Simulator — Archipelago Integration Plugin
 *
 * This DLL is loaded by ATS via the SCS SDK plugin system. It:
 *  1. Receives game events (job delivered, level-up, city visited, etc.)
 *  2. Writes those events to a JSON file the Python client reads.
 *  3. Reads the unlocked-items JSON file the Python client writes.
 *  4. Triggers a quick-load (F9) when the Python client signals it has
 *     patched the save file with pending XP / money grants.
 *
 * Build requirements:
 *  - Windows x64 (ATS is Windows-only)
 *  - SCS SDK headers (download from https://modding.scssoft.com/wiki/SDK)
 *  - C++17 or later
 *  - nlohmann/json (single-header, included in /vendor/nlohmann/json.hpp)
 *
 * Install: copy ats_archipelago.dll to
 *     <Steam>\steamapps\common\American Truck Simulator\bin\win_x64\plugins\
 *
 * See plugin/INSTALL.md for full build and install instructions.
 */

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shlobj.h>    // SHGetFolderPathW
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <set>
#include <map>
#include <mutex>
#include <chrono>
#include <ctime>
#include <sstream>
#include <iomanip>

// SCS SDK headers — obtain from https://modding.scssoft.com/wiki/SDK
#include "scssdk_telemetry.h"
#include "eurotrucks2/scssdk_eut2.h"
#include "eurotrucks2/scssdk_telemetry_eut2.h"
#include "amtrucks/scssdk_ats.h"
#include "amtrucks/scssdk_telemetry_ats.h"

// nlohmann/json single-header — https://github.com/nlohmann/json (MIT license)
#include "nlohmann/json.hpp"

using json = nlohmann::json;
namespace fs = std::filesystem;

// ── Plugin version ─────────────────────────────────────────────────────────────
static const char* PLUGIN_VERSION = "1.0.0";

// ── Communication file paths ───────────────────────────────────────────────────
static fs::path g_comm_dir;
static fs::path g_events_file;
static fs::path g_items_file;

// ── SCS logging function ───────────────────────────────────────────────────────
static scs_log_t g_log = nullptr;

inline void log(const std::string& msg, scs_log_type_t level = SCS_LOG_TYPE_message) {
    if (g_log) {
        g_log(level, ("[ATS-AP] " + msg).c_str());
    }
}

// ── Shared game state ──────────────────────────────────────────────────────────
static std::mutex g_state_mutex;

struct GameState {
    bool plugin_alive = true;
    int  current_level = 0;
    long long current_money = 0;
    float truck_x = 0.0f;
    float truck_y = 0.0f;
    float truck_z = 0.0f;
    std::string current_city_id;
    std::string current_cargo_id;
    std::string current_cargo_name;
    std::string job_source_city;
    std::string job_dest_city;
    bool job_active = false;
    bool in_game = false;
};

static GameState g_state;

// ── Item state (read from items.json) ─────────────────────────────────────────
struct ItemState {
    std::set<std::string> unlocked_trucks;
    std::map<std::string, int> upgrade_tiers;
    bool shuffle_trucks = false;
    bool shuffle_truck_upgrades = false;
    int win_condition = 0;
    int goal_level = 35;
    long long goal_money = 1000000;
    double last_read_time = 0.0;
};

static ItemState g_items;

// ── Event queue (flushed to events.json) ──────────────────────────────────────
struct GameEvent {
    std::string id;        // unique ID to prevent duplicate processing
    std::string type;
    std::string game_id;
    json extra;            // arbitrary extra data per event type
};

static std::vector<GameEvent> g_event_queue;
static std::set<std::string>  g_sent_event_ids;  // prevent re-queuing


// ── Helpers ────────────────────────────────────────────────────────────────────

static double now_seconds() {
    return static_cast<double>(
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now().time_since_epoch()
        ).count()
    ) / 1000.0;
}

static std::string make_event_id(const std::string& type, const std::string& key) {
    return type + "::" + key;
}

static fs::path get_documents_path() {
    wchar_t path[MAX_PATH];
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_PERSONAL, nullptr, SHGFP_TYPE_CURRENT, path))) {
        return fs::path(path);
    }
    return fs::path(getenv("USERPROFILE")) / "Documents";
}

static std::string hex_addr(uintptr_t addr) {
    std::ostringstream oss;
    oss << "0x" << std::hex << std::uppercase << addr;
    return oss.str();
}

// ── Quick-load trigger ─────────────────────────────────────────────────────────
// Python patches the save file with pending XP/money grants, writes the
// quicksave slot, then increments reload_counter in items.json.  The DLL
// sends F9 to the ATS window so the game loads the patched quicksave.

static int  g_last_reload_counter  = 0;
static bool g_reload_counter_synced = false;  // true after first items.json read

static HWND find_ats_window() {
    struct EnumData { DWORD pid; HWND hwnd; };
    EnumData d = { GetCurrentProcessId(), nullptr };

    EnumWindows([](HWND hwnd, LPARAM lp) -> BOOL {
        auto* data = reinterpret_cast<EnumData*>(lp);
        if (!IsWindowVisible(hwnd)) return TRUE;
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        if (pid == data->pid) {
            data->hwnd = hwnd;
            return FALSE;
        }
        return TRUE;
    }, reinterpret_cast<LPARAM>(&d));

    return d.hwnd;
}

static void trigger_quick_load() {
    HWND hwnd = find_ats_window();
    if (!hwnd) {
        log("quick_load: could not find ATS window", SCS_LOG_TYPE_warning);
        return;
    }

    UINT scan      = MapVirtualKeyA(VK_F9, MAPVK_VK_TO_VSC);
    LPARAM lp_down = 1 | (scan << 16);
    LPARAM lp_up   = 1 | (scan << 16) | (1 << 30) | (1 << 31);

    PostMessageA(hwnd, WM_KEYDOWN, VK_F9, lp_down);
    PostMessageA(hwnd, WM_KEYUP,   VK_F9, lp_up);

    log("quick_load: F9 sent to window " +
        hex_addr(reinterpret_cast<uintptr_t>(hwnd)));
}

// ── Read items.json (written by Python client) ─────────────────────────────────
static void read_items_file() {
    int  reload_counter = 0;
    bool in_game        = false;

    {
        std::lock_guard<std::mutex> lock(g_state_mutex);

        if (!fs::exists(g_items_file)) return;

        try {
            std::ifstream f(g_items_file);
            json j = json::parse(f);

            g_items.unlocked_trucks.clear();
            for (auto& t : j.value("unlocked_trucks", json::array())) {
                g_items.unlocked_trucks.insert(t.get<std::string>());
            }

            g_items.shuffle_trucks         = j.value("shuffle_trucks",         false);
            g_items.shuffle_truck_upgrades = j.value("shuffle_truck_upgrades", false);
            g_items.win_condition          = j.value("win_condition",           0);
            g_items.goal_level             = j.value("goal_level",              35);
            g_items.goal_money             =
                (long long)(j.value("goal_money_thousands", 1000)) * 1000;

            g_items.upgrade_tiers.clear();
            if (j.contains("upgrade_tiers") && j["upgrade_tiers"].is_object()) {
                for (auto& [k, v] : j["upgrade_tiers"].items()) {
                    if (v.is_number_integer())
                        g_items.upgrade_tiers[k] = v.get<int>();
                }
            }

            if (j.contains("item_notifications") && j["item_notifications"].is_array()) {
                std::size_t n = j["item_notifications"].size();
                if (n > 0)
                    log("items.json: " + std::to_string(n) +
                        " notification(s) queued for Lua mod");
            }

            reload_counter = j.value("reload_counter", 0);
            in_game        = g_state.in_game;
            g_items.last_read_time = now_seconds();

        } catch (const std::exception& e) {
            log(std::string("Failed to read items.json: ") + e.what(),
                SCS_LOG_TYPE_warning);
            return;
        }
    }

    // On the first read after plugin init, sync the counter without triggering
    // a reload — grants from previous sessions are already in the loaded save.
    if (!g_reload_counter_synced) {
        g_reload_counter_synced = true;
        g_last_reload_counter   = reload_counter;
        return;
    }

    if (reload_counter > g_last_reload_counter) {
        g_last_reload_counter = reload_counter;
        if (in_game) {
            trigger_quick_load();
        } else {
            log("quick_load: reload requested but simulation not running — "
                "will apply when player is in game");
        }
    }
}

// ── Write events.json (read by Python client) ──────────────────────────────────
static void flush_events_file() {
    std::lock_guard<std::mutex> lock(g_state_mutex);

    json j;
    j["plugin_alive"]   = g_state.plugin_alive;
    j["plugin_version"] = PLUGIN_VERSION;
    j["timestamp"]      = now_seconds();
    j["current_level"]  = g_state.current_level;
    j["current_money"]  = g_state.current_money;
    j["truck_position"] = {g_state.truck_x, g_state.truck_y, g_state.truck_z};
    j["in_game"]        = g_state.in_game;

    json events = json::array();
    for (const auto& ev : g_event_queue) {
        json entry;
        entry["id"]      = ev.id;
        entry["type"]    = ev.type;
        entry["game_id"] = ev.game_id;
        for (auto& [k, v] : ev.extra.items()) {
            entry[k] = v;
        }
        events.push_back(entry);
    }
    j["events"] = events;

    // Atomic write via temp file
    fs::path tmp = g_events_file;
    tmp += ".tmp";
    try {
        std::ofstream f(tmp);
        f << j.dump(2);
        f.close();
        fs::rename(tmp, g_events_file);
    } catch (const std::exception& e) {
        log(std::string("Failed to write events.json: ") + e.what(),
            SCS_LOG_TYPE_warning);
    }
}

// ── Queue a game event ─────────────────────────────────────────────────────────
static void queue_event(const std::string& type, const std::string& game_id,
                        const std::string& key_suffix, json extra = json::object()) {
    std::string eid = make_event_id(type, key_suffix);
    if (g_sent_event_ids.count(eid)) return;
    g_sent_event_ids.insert(eid);
    g_event_queue.push_back({eid, type, game_id, std::move(extra)});
}

// ── SCS SDK telemetry callbacks ────────────────────────────────────────────────

SCSAPI_VOID telemetry_gameplay_event(const scs_event_t event,
                                     const void* const event_info,
                                     const scs_context_t context) {
    if (!event_info) return;
    const scs_telemetry_gameplay_event_t* const gev =
        static_cast<const scs_telemetry_gameplay_event_t*>(event_info);
    if (!gev->id) return;
    const std::string event_name(gev->id);

    if (event_name == SCS_TELEMETRY_GAMEPLAY_EVENT_job_delivered) {
        std::lock_guard<std::mutex> lock(g_state_mutex);

        if (!g_state.current_cargo_id.empty()) {
            json extra;
            extra["cargo_name"] = g_state.current_cargo_name;
            queue_event("cargo_delivered", g_state.current_cargo_id,
                        g_state.current_cargo_id, extra);
        }

        flush_events_file();
    }

    if (event_name == SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled) {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.job_active = false;
    }
}

// Channel callback — position tracking
SCSAPI_VOID on_truck_placement(const scs_string_t name,
                                const scs_u32_t index,
                                const scs_value_t* const value,
                                const scs_context_t context) {
    if (!value || value->type != SCS_VALUE_TYPE_dplacement) return;
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.truck_x = static_cast<float>(value->value_dplacement.position.x);
    g_state.truck_y = static_cast<float>(value->value_dplacement.position.y);
    g_state.truck_z = static_cast<float>(value->value_dplacement.position.z);
}

// ── Configuration event (fires when job starts/clears) ────────────────────────
SCSAPI_VOID telemetry_configuration(const scs_event_t event,
                                     const void* const event_info,
                                     const scs_context_t context) {
    if (!event_info) return;
    const scs_telemetry_configuration_t* const cfg =
        static_cast<const scs_telemetry_configuration_t*>(event_info);
    if (!cfg->id) return;

    if (std::string(cfg->id) != SCS_TELEMETRY_CONFIG_job) return;

    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.current_cargo_id.clear();
    g_state.current_cargo_name.clear();
    g_state.job_active = false;

    for (const scs_named_value_t* attr = cfg->attributes; attr->name != nullptr; ++attr) {
        const std::string attr_name(attr->name);
        if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id) {
            if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value) {
                g_state.current_cargo_id  = attr->value.value_string.value;
                g_state.job_active        = true;
            }
        } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo) {
            if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value) {
                g_state.current_cargo_name = attr->value.value_string.value;
            }
        }
    }

    if (g_state.job_active) {
        log("Job started: cargo=" + g_state.current_cargo_id +
            " name=" + g_state.current_cargo_name);
    }
}

// ── Paused / started events ───────────────────────────────────────────────────
SCSAPI_VOID telemetry_paused(const scs_event_t event,
                              const void* const event_info,
                              const scs_context_t context) {
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.in_game = false;
}

SCSAPI_VOID telemetry_started(const scs_event_t event,
                               const void* const event_info,
                               const scs_context_t context) {
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.in_game = true;
    // Allow a pending reload request to fire now that simulation is running.
    // (The counter check in read_items_file handles the actual trigger.)
}

// ── Frame-timing state (declared here so discovery code can reference g_startup_time) ──
static double g_last_poll_time    = 0.0;
static double g_last_flush_time   = 0.0;
static double g_startup_time      = 0.0;
static const double POLL_INTERVAL_SECONDS  = 2.0;
static const double FLUSH_INTERVAL_SECONDS = 2.0;

// ── Memory discovery ───────────────────────────────────────────────────────────
// When "discovery_mode.txt" exists in the comm folder, the DLL scans its own
// heap memory for garage and truck strings and logs offsets to game.log.
// This is a developer tool for mapping object layouts for future features.

static bool g_discovery_done = false;

struct ScanMatch {
    uintptr_t   address;
    std::string target;
    std::vector<std::pair<int, uint32_t>> nearby_vals;
    std::string hex_context;
};

static bool safe_read32(const uint8_t* src, uint32_t& out) {
    memcpy(&out, src, 4);
    return true;
}

static std::vector<ScanMatch> scan_heap_for_strings(
        const std::vector<std::string>& targets,
        size_t   max_results = 200,
        int      near_range  = 128,
        uint32_t min_val     = 1,
        uint32_t max_val     = 10,
        bool     include_hex = false)
{
    std::vector<ScanMatch> results;
    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = 0x10000;

    while (results.size() < max_results &&
           VirtualQuery((LPCVOID)addr, &mbi, sizeof(mbi)) == sizeof(mbi))
    {
        bool scannable = (mbi.State   == MEM_COMMIT)   &&
                         (mbi.Type    == MEM_PRIVATE)   &&
                         (mbi.Protect == PAGE_READWRITE) &&
                         (mbi.RegionSize > 0)             &&
                         (mbi.RegionSize < 128 * 1024 * 1024);

        if (scannable) {
            const uint8_t* region = reinterpret_cast<const uint8_t*>(mbi.BaseAddress);
            size_t         rsize  = mbi.RegionSize;

            for (const auto& tgt : targets) {
                if (results.size() >= max_results) break;
                size_t tlen = tgt.size();

                for (size_t i = 0; i + tlen + 1 < rsize; ++i) {
                    if (region[i] != (uint8_t)tgt[0]) continue;
                    if (region[i + tlen] != '\0')      continue;
                    if (memcmp(region + i, tgt.c_str(), tlen) != 0) continue;

                    ScanMatch m;
                    m.address = reinterpret_cast<uintptr_t>(region + i);
                    m.target  = tgt;

                    for (int off = -near_range; off < near_range; off += 4) {
                        intptr_t abs = static_cast<intptr_t>(i) + off;
                        if (abs < 0 || abs + 4 > static_cast<intptr_t>(rsize)) continue;
                        uint32_t v = 0;
                        if (safe_read32(region + abs, v) && v >= min_val && v <= max_val)
                            m.nearby_vals.push_back({off, v});
                    }

                    if (include_hex) {
                        std::ostringstream hex;
                        for (int h = -16; h < 32; ++h) {
                            if (h == 0) hex << "|";
                            intptr_t abs_h = static_cast<intptr_t>(i) + h;
                            if (abs_h >= 0 && abs_h < static_cast<intptr_t>(rsize))
                                hex << std::hex << std::setw(2) << std::setfill('0')
                                    << static_cast<int>(region[abs_h]);
                            else
                                hex << "??";
                        }
                        m.hex_context = hex.str();
                    }

                    results.push_back(std::move(m));
                    if (results.size() >= max_results) break;
                }
            }
        }

        if (mbi.RegionSize == 0) break;
        addr += mbi.RegionSize;
    }

    return results;
}

static void log_match(const std::string& prefix, const ScanMatch& m) {
    std::ostringstream oss;
    oss << prefix << "[" << m.target << "] 0x"
        << std::hex << std::setw(12) << std::setfill('0') << m.address
        << std::dec;
    for (const auto& [off, v] : m.nearby_vals)
        oss << "  " << (off >= 0 ? "+" : "") << off << "=" << v;
    log(oss.str());
    if (!m.hex_context.empty())
        log("  hex: " + m.hex_context);
}

static void run_discovery() {
    log("=== ATS-AP DISCOVERY MODE START ===");
    log("Scanning heap for garage/office/truck objects. Share game.log with the developer.");

    std::vector<std::string> garage_targets = {
        "garage.san_francisco",  "garage.los_angeles",
        "garage.sacramento",     "garage.fresno",
        "garage.bakersfield",    "garage.stockton",
        "garage.eureka",         "garage.redding",
        "garage.san_diego",      "garage.las_vegas",
        "garage.reno",           "garage.elko",
        "garage.flagstaff",      "garage.phoenix",
        "garage.tucson",         "garage.prescott",
    };
    auto garage_matches = scan_heap_for_strings(garage_targets, 100, 256, 1, 10, true);
    log("--- GARAGE RESULTS (" + std::to_string(garage_matches.size()) + " matches) ---");
    for (const auto& m : garage_matches) log_match("G", m);

    std::vector<std::string> office_targets = {
        "recruitment_agency.san_francisco", "recruitment_agency.los_angeles",
        "recruitment_agency.sacramento",    "recruitment_agency.fresno",
        "recruitment_agency.bakersfield",   "recruitment_agency.stockton",
        "recruitment_agency.eureka",        "recruitment_agency.redding",
        "recruitment_agency.san_diego",     "recruitment_agency.las_vegas",
        "recruitment_agency.reno",          "recruitment_agency.elko",
        "recruitment_agency.flagstaff",     "recruitment_agency.phoenix",
        "recruitment_agency.tucson",        "recruitment_agency.prescott",
    };
    auto office_matches = scan_heap_for_strings(office_targets, 100, 256, 1, 10, true);
    log("--- OFFICE RESULTS (" + std::to_string(office_matches.size()) + " matches) ---");
    for (const auto& m : office_matches) log_match("O", m);

    std::vector<std::string> truck_targets = {
        "kenworth_w900",    "kenworth_t800",    "kenworth_t660",  "kenworth_k100e",
        "peterbilt_389",    "peterbilt_388",    "peterbilt_367",
        "western_star_49x", "western_star_57x",
        "freightliner_114sd", "freightliner_coronado",
        "mack_anthem",      "mack_pinnacle",
        "international_lt",
        "vehicle.kenworth_w900",    "vehicle.peterbilt_389",
        "vehicle.western_star_49x", "vehicle.freightliner_coronado",
    };
    auto truck_matches = scan_heap_for_strings(truck_targets, 100, 128, 1, 10, false);
    log("--- TRUCK RESULTS (" + std::to_string(truck_matches.size()) + " matches) ---");
    for (const auto& m : truck_matches) log_match("T", m);

    log("=== ATS-AP DISCOVERY MODE END ===");
    g_discovery_done = true;
}

static void maybe_run_discovery(double now) {
    if (g_discovery_done) return;
    if (now - g_startup_time < 15.0) return;  // wait for game to finish loading
    fs::path flag = g_comm_dir / "discovery_mode.txt";
    if (!fs::exists(flag)) return;
    run_discovery();
    try { fs::remove(flag); } catch (...) {}
}

// ── Frame callback — drives all periodic operations ───────────────────────────

SCSAPI_VOID telemetry_frame_start(const scs_event_t event,
                                   const void* const event_info,
                                   const scs_context_t context) {
    double t = now_seconds();

    maybe_run_discovery(t);

    if (t - g_last_poll_time >= POLL_INTERVAL_SECONDS) {
        g_last_poll_time = t;
        read_items_file();
    }

    if (t - g_last_flush_time >= FLUSH_INTERVAL_SECONDS) {
        g_last_flush_time = t;
        flush_events_file();
    }
}


// ── SCS SDK entry points ───────────────────────────────────────────────────────

SCSAPI_RESULT scs_telemetry_init(const scs_u32_t version,
                                  const scs_telemetry_init_params_t* const params) {
    if (version < SCS_TELEMETRY_VERSION_1_00) {
        return SCS_RESULT_unsupported;
    }

    const scs_telemetry_init_params_v100_t* const p =
        static_cast<const scs_telemetry_init_params_v100_t*>(params);

    g_log = p->common.log;
    log("Archipelago plugin initializing v" + std::string(PLUGIN_VERSION));

    g_comm_dir    = get_documents_path() / "American Truck Simulator" / "archipelago";
    g_events_file = g_comm_dir / "events.json";
    g_items_file  = g_comm_dir / "items.json";

    if (!fs::exists(g_comm_dir)) {
        fs::create_directories(g_comm_dir);
    }

    log("Communication folder: " + g_comm_dir.string());

    read_items_file();

    p->register_for_event(SCS_TELEMETRY_EVENT_frame_start,   telemetry_frame_start,    nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_paused,        telemetry_paused,         nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_started,       telemetry_started,        nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_configuration, telemetry_configuration,  nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_gameplay,      telemetry_gameplay_event, nullptr);

    p->register_for_channel(
        SCS_TELEMETRY_TRUCK_CHANNEL_world_placement,
        SCS_U32_NIL,
        SCS_VALUE_TYPE_dplacement,
        SCS_TELEMETRY_CHANNEL_FLAG_none,
        on_truck_placement,
        nullptr
    );

    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.plugin_alive = true;
        g_state.in_game = false;
    }
    flush_events_file();

    g_startup_time = now_seconds();
    log("Archipelago plugin initialized. Waiting for Python client...");
    return SCS_RESULT_ok;
}

SCSAPI_VOID scs_telemetry_shutdown() {
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.plugin_alive = false;
    }
    flush_events_file();
    log("Archipelago plugin shutdown.");
}


// ── DLL entry point ────────────────────────────────────────────────────────────
BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    return TRUE;
}
