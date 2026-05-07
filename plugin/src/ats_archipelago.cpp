/**
 * ats_archipelago.cpp
 *
 * American Truck Simulator — Archipelago Integration Plugin
 *
 * This DLL is loaded by ATS via the SCS SDK plugin system. It:
 *  1. Receives game events (job delivered, level-up, city visited, etc.)
 *  2. Writes those events to a JSON file the Python client reads.
 *  3. Reads the unlocked-items JSON file the Python client writes.
 *  4. Filters job acceptance: jobs to/from cities whose garage/office is locked
 *     are handled by the Lua mod layer.
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
#include <atomic>

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
    int64_t total_money_granted = 0;
    int32_t total_xp_granted = 0;
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

// ── Game module base ───────────────────────────────────────────────────────────
// Cached at init time. GetModuleHandleA(nullptr) returns the base address of the
// host process (amtrucks.exe). Hardcoding the exe name is fragile and wrong here —
// the process is amtrucks.exe, not amtrucks_x64.exe.
static uintptr_t g_module_base = 0;

static void init_module_base() {
    g_module_base = reinterpret_cast<uintptr_t>(GetModuleHandleA(nullptr));
    if (g_module_base == 0) {
        log("WARNING: could not obtain module base address — XP/money grants will not work", SCS_LOG_TYPE_warning);
    } else {
        log("Module base: " + hex_addr(g_module_base));
    }
}
// The DLL must not double-grant money/XP that was already applied to a previous
// game session. We persist applied totals in grants.json so they survive restarts.
static int64_t g_applied_money = 0;
static int32_t g_applied_xp   = 0;
static fs::path g_grants_file;

static void load_grants_file() {
    if (!fs::exists(g_grants_file)) return;
    try {
        std::ifstream f(g_grants_file);
        json j = json::parse(f);
        g_applied_money = j.value("applied_money", (int64_t)0);
        g_applied_xp    = j.value("applied_xp",    (int32_t)0);
        log("grants.json loaded: applied_money=" + std::to_string(g_applied_money) +
            " applied_xp=" + std::to_string(g_applied_xp));
    } catch (const std::exception& e) {
        log(std::string("Failed to read grants.json: ") + e.what(), SCS_LOG_TYPE_warning);
    }
}

static void save_grants_file() {
    json j;
    j["applied_money"] = g_applied_money;
    j["applied_xp"]    = g_applied_xp;
    fs::path tmp = g_grants_file;
    tmp += ".tmp";
    try {
        std::ofstream f(tmp);
        f << j.dump(2);
        f.close();
        fs::rename(tmp, g_grants_file);
    } catch (const std::exception& e) {
        log(std::string("Failed to write grants.json: ") + e.what(), SCS_LOG_TYPE_warning);
    }
}

// ── Pointer chain helper ───────────────────────────────────────────────────────
// Follows a Cheat Engine-style multi-level pointer chain rooted at the game module.
//
//   chain[0] = static offset from module base → address of first pointer
//   chain[1..n-2] = after each dereference, add this offset before next deref
//   chain[n-1] = final offset added after last dereference → value address
//
// Returns the address of the final value, or 0 on failure.
static uintptr_t follow_chain(uintptr_t module_base, uintptr_t static_offset,
                               const std::vector<uintptr_t>& deref_offsets,
                               uintptr_t final_offset)
{
    uintptr_t ptr = module_base + static_offset;

    // Read first pointer
    MEMORY_BASIC_INFORMATION mbi;
    if (VirtualQuery(reinterpret_cast<LPCVOID>(ptr), &mbi, sizeof(mbi)) != sizeof(mbi)) return 0;
    if (mbi.State != MEM_COMMIT) return 0;
    uintptr_t p = 0;
    memcpy(&p, reinterpret_cast<void*>(ptr), sizeof(p));
    if (p == 0) return 0;

    // Follow each intermediate offset: add → deref
    for (uintptr_t off : deref_offsets) {
        ptr = p + off;
        if (VirtualQuery(reinterpret_cast<LPCVOID>(ptr), &mbi, sizeof(mbi)) != sizeof(mbi)) return 0;
        if (mbi.State != MEM_COMMIT) return 0;
        p = 0;
        memcpy(&p, reinterpret_cast<void*>(ptr), sizeof(p));
        if (p == 0) return 0;
    }

    return p + final_offset;
}

// ── In-process money and XP grants ────────────────────────────────────────────
// NOTE: These are currently stubs pending a Lua-based implementation.
// Memory-injection approaches proved unstable; we will use the ATS Lua scripting
// API (discovered by reading an existing XP/money mod) instead.

static bool grant_money_memory(int64_t amount) {
    static bool s_warned = false;
    if (!s_warned) { s_warned = true;
        log("grant_money: Lua-based grants not yet implemented — pending mod analysis",
            SCS_LOG_TYPE_warning);
    }
    return false;
}

static bool grant_xp_memory(int32_t amount) {
    static bool s_warned = false;
    if (!s_warned) { s_warned = true;
        log("grant_xp: Lua-based grants not yet implemented — pending mod analysis",
            SCS_LOG_TYPE_warning);
    }
    return false;
}

// ── Read items.json (written by Python client) ─────────────────────────────────
static void read_items_file() {
    int64_t want_money = 0;
    int32_t want_xp    = 0;
    bool    in_game    = false;

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

            g_items.shuffle_trucks        = j.value("shuffle_trucks", false);
            g_items.shuffle_truck_upgrades = j.value("shuffle_truck_upgrades", false);
            g_items.win_condition          = j.value("win_condition", 0);
            g_items.goal_level             = j.value("goal_level", 35);
            g_items.goal_money             = (long long)(j.value("goal_money_thousands", 1000)) * 1000;
            g_items.total_money_granted    = j.value("total_money_granted", (int64_t)0);
            g_items.total_xp_granted       = j.value("total_xp_granted",   (int32_t)0);

            g_items.upgrade_tiers.clear();
            if (j.contains("upgrade_tiers") && j["upgrade_tiers"].is_object()) {
                for (auto& [k, v] : j["upgrade_tiers"].items()) {
                    if (v.is_number_integer()) {
                        g_items.upgrade_tiers[k] = v.get<int>();
                    }
                }
            }

            if (j.contains("item_notifications") && j["item_notifications"].is_array()) {
                std::size_t n = j["item_notifications"].size();
                if (n > 0) {
                    log("items.json: " + std::to_string(n) + " notification(s) queued for Lua mod");
                }
            }

            want_money = g_items.total_money_granted;
            want_xp    = g_items.total_xp_granted;
            in_game    = g_state.in_game;

            g_items.last_read_time = now_seconds();
        } catch (const std::exception& e) {
            log(std::string("Failed to read items.json: ") + e.what(), SCS_LOG_TYPE_warning);
            return;
        }
    }

    // Apply money/XP grant deltas when the simulation is running.
    // We gate on in_game so the save file is loaded before we write to memory.
    if (!in_game) return;

    bool grants_changed = false;

    int64_t money_delta = want_money - g_applied_money;
    if (money_delta > 0) {
        if (grant_money_memory(money_delta)) {
            g_applied_money = want_money;
            grants_changed = true;
        }
    }

    int32_t xp_delta = want_xp - g_applied_xp;
    if (xp_delta > 0) {
        if (grant_xp_memory(xp_delta)) {
            g_applied_xp = want_xp;
            grants_changed = true;
        }
    }

    if (grants_changed) {
        save_grants_file();
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
        log(std::string("Failed to write events.json: ") + e.what(), SCS_LOG_TYPE_warning);
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
}

// ── Frame callback — drives all periodic operations ───────────────────────────
static double g_last_poll_time    = 0.0;
static double g_last_flush_time   = 0.0;
static double g_startup_time      = 0.0;
static const double POLL_INTERVAL_SECONDS  = 5.0;
static const double FLUSH_INTERVAL_SECONDS = 2.0;

// ── Memory discovery ───────────────────────────────────────────────────────────
// When "discovery_mode.txt" exists in the comm folder, the DLL scans its own
// heap memory for garage and truck strings and logs offsets to game.log.
// This reveals the memory layout needed to implement garage granting and truck
// hiding without requiring external Cheat Engine work.

static bool g_discovery_done = false;

struct ScanMatch {
    uintptr_t   address;
    std::string target;
    // nearby_vals[i] = {byte_offset_from_string_start, uint32_value}
    std::vector<std::pair<int, uint32_t>> nearby_vals;
    std::string hex_context;  // raw bytes [-16..+32) from string start, "|" marks offset 0
};

// Read 4 bytes from src into out. We only call this after VirtualQuery confirms
// the region is PAGE_READWRITE + MEM_COMMIT, so direct memcpy is safe.
static bool safe_read32(const uint8_t* src, uint32_t& out) {
    memcpy(&out, src, 4);
    return true;
}

// near_range: scan ±N bytes from string start (must be multiple of 4)
// min_val / max_val: only record nearby uint32 values in [min_val, max_val]
// include_hex: if true, capture raw bytes [-16..+32) around string start
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
        // Only private read-write heap pages; skip very large regions (mapped files).
        bool scannable = (mbi.State   == MEM_COMMIT)  &&
                         (mbi.Type    == MEM_PRIVATE)  &&
                         (mbi.Protect == PAGE_READWRITE) &&
                         (mbi.RegionSize > 0)            &&
                         (mbi.RegionSize < 128 * 1024 * 1024);

        if (scannable) {
            const uint8_t* region = reinterpret_cast<const uint8_t*>(mbi.BaseAddress);
            size_t         rsize  = mbi.RegionSize;

            for (const auto& tgt : targets) {
                if (results.size() >= max_results) break;
                size_t tlen = tgt.size();

                for (size_t i = 0; i + tlen + 1 < rsize; ++i) {
                    // Region is confirmed PAGE_READWRITE by VirtualQuery above.
                    if (region[i] != (uint8_t)tgt[0]) continue;
                    if (region[i + tlen] != '\0')      continue;
                    if (memcmp(region + i, tgt.c_str(), tlen) != 0) continue;

                    ScanMatch m;
                    m.address = reinterpret_cast<uintptr_t>(region + i);
                    m.target  = tgt;

                    // Sample every 4 bytes in ±near_range, record values in [min_val, max_val].
                    for (int off = -near_range; off < near_range; off += 4) {
                        intptr_t abs = static_cast<intptr_t>(i) + off;
                        if (abs < 0 || abs + 4 > static_cast<intptr_t>(rsize)) continue;
                        uint32_t v = 0;
                        if (safe_read32(region + abs, v) && v >= min_val && v <= max_val) {
                            m.nearby_vals.push_back({off, v});
                        }
                    }

                    // Raw hex dump: 16 bytes before + 32 at/after string start.
                    // "|" marks offset 0 (start of the string itself).
                    if (include_hex) {
                        std::ostringstream hex;
                        for (int h = -16; h < 32; ++h) {
                            if (h == 0) hex << "|";
                            intptr_t abs_h = static_cast<intptr_t>(i) + h;
                            if (abs_h >= 0 && abs_h < static_cast<intptr_t>(rsize)) {
                                hex << std::hex << std::setw(2) << std::setfill('0')
                                    << static_cast<int>(region[abs_h]);
                            } else {
                                hex << "??";
                            }
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
    for (const auto& [off, v] : m.nearby_vals) {
        oss << "  " << (off >= 0 ? "+" : "") << off << "=" << v;
    }
    log(oss.str());
    if (!m.hex_context.empty()) {
        log("  hex: " + m.hex_context);
    }
}

static void run_discovery() {
    log("=== ATS-AP DISCOVERY MODE START ===");
    log("Scanning heap for garage/office/truck objects. Share game.log with the developer.");

    // ── Garage compound IDs only ───────────────────────────────────────────────
    // Bare city IDs ("san_francisco") land in navigation/routing arrays (32-byte
    // stride, values 3-10) — those are NOT ownership objects.
    // The "garage.X" compound IDs are the actual save-game garage records.
    // San Francisco is the OWNED garage (home base); all others should be status 0.
    std::vector<std::string> garage_targets = {
        "garage.san_francisco",  // OWNED — must show a non-zero status nearby
        "garage.los_angeles",    // not owned
        "garage.sacramento",     "garage.fresno",
        "garage.bakersfield",    "garage.stockton",
        "garage.eureka",         "garage.redding",
        "garage.san_diego",      "garage.las_vegas",
        "garage.reno",           "garage.elko",
        "garage.flagstaff",      "garage.phoenix",
        "garage.tucson",         "garage.prescott",
    };

    // Wide range (±256), values 1–10, with hex dump to see raw object layout.
    auto garage_matches = scan_heap_for_strings(garage_targets, 100, 256, 1, 10, true);

    log("--- GARAGE RESULTS (" + std::to_string(garage_matches.size()) + " matches) ---");
    for (const auto& m : garage_matches) log_match("G", m);

    // ── Recruitment office IDs ─────────────────────────────────────────────────
    // "recruitment_agency.X" is the canonical object prefix (confirmed by scan 1).
    // All offices will show zeros until the player drives past one (status 0 = undiscovered).
    // Run a second scan AFTER visiting at least one office to see the flag flip.
    std::vector<std::string> office_targets = {
        "recruitment_agency.san_francisco",
        "recruitment_agency.los_angeles",
        "recruitment_agency.sacramento",
        "recruitment_agency.fresno",
        "recruitment_agency.bakersfield",
        "recruitment_agency.stockton",
        "recruitment_agency.eureka",
        "recruitment_agency.redding",
        "recruitment_agency.san_diego",
        "recruitment_agency.las_vegas",
        "recruitment_agency.reno",
        "recruitment_agency.elko",
        "recruitment_agency.flagstaff",
        "recruitment_agency.phoenix",
        "recruitment_agency.tucson",
        "recruitment_agency.prescott",
    };

    auto office_matches = scan_heap_for_strings(office_targets, 100, 256, 1, 10, true);

    log("--- OFFICE RESULTS (" + std::to_string(office_matches.size()) + " matches) ---");
    for (const auto& m : office_matches) log_match("O", m);

    // ── Truck model IDs ────────────────────────────────────────────────────────
    std::vector<std::string> truck_targets = {
        "kenworth_w900", "kenworth_t800", "kenworth_t660", "kenworth_k100e",
        "peterbilt_389", "peterbilt_388", "peterbilt_367",
        "western_star_49x", "western_star_57x",
        "freightliner_114sd", "freightliner_coronado",
        "mack_anthem", "mack_pinnacle",
        "international_lt",
        "vehicle.kenworth_w900", "vehicle.peterbilt_389",
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
    // Wait 15 s after plugin init so the game has fully loaded the save.
    if (now - g_startup_time < 15.0) return;

    fs::path flag = g_comm_dir / "discovery_mode.txt";
    if (!fs::exists(flag)) return;  // Keep checking every frame — file may appear later.

    run_discovery();
    try { fs::remove(flag); } catch (...) {}
}

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
    init_module_base();

    g_comm_dir    = get_documents_path() / "American Truck Simulator" / "archipelago";
    g_events_file = g_comm_dir / "events.json";
    g_items_file  = g_comm_dir / "items.json";
    g_grants_file = g_comm_dir / "grants.json";

    if (!fs::exists(g_comm_dir)) {
        fs::create_directories(g_comm_dir);
    }

    log("Communication folder: " + g_comm_dir.string());

    load_grants_file();
    read_items_file();

    p->register_for_event(SCS_TELEMETRY_EVENT_frame_start,   telemetry_frame_start,   nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_paused,        telemetry_paused,        nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_started,       telemetry_started,       nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_configuration, telemetry_configuration, nullptr);
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
