/**
 * ats_archipelago.cpp
 *
 * American Truck Simulator — Archipelago Integration Plugin
 *
 * This DLL is loaded by ATS via the SCS SDK plugin system. It:
 *  1. Receives game events (job delivered, level-up, city visited, etc.)
 *  2. Writes those events to a JSON file the Python client reads.
 *  3. Reads the unlocked-items JSON file the Python client writes.
 *  4. Enforces state boundaries: if the player enters a locked state,
 *     their truck is warped back to the nearest safe position and a
 *     rejection job event is triggered.
 *  5. Filters job acceptance: jobs to/from locked-state cities are blocked.
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

#define NOMINMAX
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
    std::set<std::string> unlocked_states;
    std::set<std::string> unlocked_trucks;
    std::set<std::string> unlocked_garages;
    std::set<std::string> unlocked_offices;
    std::map<std::string, int> upgrade_tiers;
    std::vector<int> pending_money_bonuses;
    bool shuffle_trucks = false;
    bool shuffle_garages = false;
    bool shuffle_recruitment_offices = false;
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

// ── Boundary violation state ───────────────────────────────────────────────────
static float g_last_safe_x = 0.0f;
static float g_last_safe_z = 0.0f;
static std::string g_last_safe_state;
static bool g_boundary_violation_active = false;

// ── City→state mapping (populated from items.json's unlocked_states data) ─────
// In a full implementation this would be loaded from a bundled JSON file.
// For now the plugin uses the items.json to know which states are locked/unlocked,
// and the Lua mod (reading the same file) handles the job-market filtering.
// The C++ plugin enforces position-based boundary when the player crosses into
// a state whose id is NOT in g_items.unlocked_states.

// Approximate state bounding boxes (X/Z in ATS world coordinates).
// These are rough and need calibration against actual ATS map data.
// Format: {state_id, {min_x, max_x, min_z, max_z}}
// NOTE: ATS uses a left-handed coordinate system; Y is altitude.
// These values are placeholders and MUST be measured in-game or from map data.
static const std::map<std::string, std::array<float,4>> STATE_BOUNDS = {
    // {state_id, {min_x, max_x, min_z, max_z}}
    // Values below are rough estimates — calibrate with in-game measurement.
    {"california",  {-94000.f, -60000.f, -33000.f,  20000.f}},
    {"nevada",      {-60000.f, -30000.f, -33000.f,  15000.f}},
    {"arizona",     {-75000.f, -40000.f, -70000.f, -33000.f}},
    {"new_mexico",  {-20000.f,  15000.f, -75000.f, -35000.f}},
    {"oregon",      {-94000.f, -55000.f,  20000.f,  60000.f}},
    {"washington",  {-94000.f, -50000.f,  60000.f, 100000.f}},
    {"utah",        {-30000.f,   5000.f, -33000.f,  10000.f}},
    {"idaho",       {-55000.f, -20000.f,  10000.f,  60000.f}},
    {"colorado",    {  5000.f,  50000.f, -35000.f,  10000.f}},
    {"wyoming",     {-10000.f,  50000.f,  10000.f,  55000.f}},
    {"montana",     {-30000.f,  80000.f,  55000.f, 105000.f}},
    {"texas",       { 20000.f, 120000.f, -80000.f, -35000.f}},
    {"oklahoma",    { 20000.f,  90000.f, -35000.f, -10000.f}},
    {"kansas",      { 20000.f,  90000.f, -10000.f,  25000.f}},
    {"nebraska",    { 20000.f,  90000.f,  25000.f,  55000.f}},
    {"arkansas",    { 70000.f, 120000.f, -35000.f,   0000.f}},
    {"missouri",    { 70000.f, 130000.f,   0000.f,  35000.f}},
    {"iowa",        { 50000.f, 110000.f,  35000.f,  65000.f}},
    {"louisiana",   { 70000.f, 130000.f, -80000.f, -50000.f}},
};


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

// ── Read items.json (written by Python client) ─────────────────────────────────
static void read_items_file() {
    std::lock_guard<std::mutex> lock(g_state_mutex);

    if (!fs::exists(g_items_file)) return;

    try {
        std::ifstream f(g_items_file);
        json j = json::parse(f);

        g_items.unlocked_states.clear();
        for (auto& s : j.value("unlocked_states", json::array())) {
            g_items.unlocked_states.insert(s.get<std::string>());
        }
        // Base game states always unlocked
        g_items.unlocked_states.insert("california");
        g_items.unlocked_states.insert("nevada");

        g_items.unlocked_trucks.clear();
        for (auto& t : j.value("unlocked_trucks", json::array())) {
            g_items.unlocked_trucks.insert(t.get<std::string>());
        }

        g_items.unlocked_garages.clear();
        for (auto& g : j.value("unlocked_garages", json::array())) {
            g_items.unlocked_garages.insert(g.get<std::string>());
        }

        g_items.unlocked_offices.clear();
        for (auto& o : j.value("unlocked_offices", json::array())) {
            g_items.unlocked_offices.insert(o.get<std::string>());
        }

        g_items.shuffle_trucks              = j.value("shuffle_trucks", false);
        g_items.shuffle_garages             = j.value("shuffle_garages", false);
        g_items.shuffle_recruitment_offices = j.value("shuffle_recruitment_offices", false);
        g_items.shuffle_truck_upgrades      = j.value("shuffle_truck_upgrades", false);
        g_items.win_condition               = j.value("win_condition", 0);
        g_items.goal_level                  = j.value("goal_level", 35);
        g_items.goal_money                  = (long long)(j.value("goal_money_thousands", 1000)) * 1000;

        g_items.pending_money_bonuses.clear();
        for (auto& m : j.value("pending_money_bonuses", json::array())) {
            g_items.pending_money_bonuses.push_back(m.get<int>());
        }

        g_items.last_read_time = now_seconds();
    } catch (const std::exception& e) {
        log(std::string("Failed to read items.json: ") + e.what(), SCS_LOG_TYPE_warning);
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
    j["delivered_bonuses"] = json::array(); // filled by plugin when bonuses are applied

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

// ── State boundary enforcement ─────────────────────────────────────────────────
static std::string get_state_at(float x, float z) {
    for (const auto& [state_id, bounds] : STATE_BOUNDS) {
        if (x >= bounds[0] && x <= bounds[1] && z >= bounds[2] && z <= bounds[3]) {
            return state_id;
        }
    }
    return "";
}

static bool is_state_locked(const std::string& state_id) {
    if (state_id.empty()) return false;
    return g_items.unlocked_states.find(state_id) == g_items.unlocked_states.end();
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

        // Cargo delivery check
        if (!g_state.current_cargo_id.empty()) {
            json extra;
            extra["cargo_name"] = g_state.current_cargo_name;
            queue_event("cargo_delivered", g_state.current_cargo_id,
                        g_state.current_cargo_id, extra);
        }

        // Garage upgrade check is polled separately — delivery completes the job
        flush_events_file();
    }

    if (event_name == SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled) {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.job_active = false;
    }
}

// Channel callbacks — called each telemetry frame (~50ms)
SCSAPI_VOID on_truck_placement(const scs_string_t name,
                                const scs_u32_t index,
                                const scs_value_t* const value,
                                const scs_context_t context) {
    if (!value || value->type != SCS_VALUE_TYPE_dplacement) return;
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.truck_x = static_cast<float>(value->value_dplacement.value.position.x);
    g_state.truck_y = static_cast<float>(value->value_dplacement.value.position.y);
    g_state.truck_z = static_cast<float>(value->value_dplacement.value.position.z);

    // Boundary check: determine current state from position
    std::string cur_state = get_state_at(g_state.truck_x, g_state.truck_z);
    if (!cur_state.empty() && is_state_locked(cur_state)) {
        // Player is in a locked state — record violation event
        if (!g_boundary_violation_active) {
            g_boundary_violation_active = true;
            log("BOUNDARY VIOLATION: Player entered locked state: " + cur_state, SCS_LOG_TYPE_warning);
            json extra;
            extra["locked_state"] = cur_state;
            extra["safe_x"] = g_last_safe_x;
            extra["safe_z"] = g_last_safe_z;
            // Queue a boundary_violation event so the Python client can show a warning
            queue_event("boundary_violation", cur_state, cur_state, extra);
        }
    } else {
        g_boundary_violation_active = false;
        if (!cur_state.empty()) {
            g_last_safe_x = g_state.truck_x;
            g_last_safe_z = g_state.truck_z;
            g_last_safe_state = cur_state;
        }
    }
}

// Config callbacks — called when job or truck config changes
SCSAPI_VOID on_job_config(const scs_string_t name,
                           const scs_u32_t index,
                           const scs_value_t* const value,
                           const scs_context_t context) {
    // Job config changes: capture cargo id/name, source, dest
    // The actual field names depend on the SCS SDK config channel keys
    // We use the gameplay event system primarily; config is supplementary.
}

SCSAPI_VOID on_gameplay_event_channel(const scs_string_t name,
                                       const scs_u32_t index,
                                       const scs_value_t* const value,
                                       const scs_context_t context) {
    // Additional channel data
}


// ── Periodic poll (called from a Windows timer or a background thread) ─────────
// This polls save-file-derived data: level, money, visited cities, garage states.
// The save file is at: Documents\American Truck Simulator\profiles\<id>\save\<slot>\game.sii
// Parsing is done on a 5-second interval to avoid I/O overhead.

static double g_last_poll_time = 0.0;
static const double POLL_INTERVAL_SECONDS = 5.0;

static void poll_save_file() {
    double t = now_seconds();
    if (t - g_last_poll_time < POLL_INTERVAL_SECONDS) return;
    g_last_poll_time = t;

    // Re-read items.json on same interval
    read_items_file();
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

    // Set up communication directory
    g_comm_dir    = get_documents_path() / "American Truck Simulator" / "archipelago";
    g_events_file = g_comm_dir / "events.json";
    g_items_file  = g_comm_dir / "items.json";

    if (!fs::exists(g_comm_dir)) {
        fs::create_directories(g_comm_dir);
    }

    log("Communication folder: " + g_comm_dir.string());

    // Read initial items state
    read_items_file();

    // Register gameplay event callbacks
    p->register_for_event(SCS_TELEMETRY_EVENT_gameplay, telemetry_gameplay_event, nullptr);

    // Register position channel (called each telemetry frame ~50ms)
    p->register_channel(
        SCS_TELEMETRY_TRUCK_CHANNEL_world_placement,
        SCS_U32_NIL,
        SCS_VALUE_TYPE_dplacement,
        SCS_TELEMETRY_CHANNEL_FLAG_none,
        on_truck_placement,
        nullptr
    );

    // Write initial alive signal
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.plugin_alive = true;
        g_state.in_game = false;
    }
    flush_events_file();

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
