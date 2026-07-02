/**
 * ats_archipelago.cpp
 *
 * American Truck Simulator — Archipelago Integration Plugin
 *
 * This DLL is loaded by ATS via the SCS SDK plugin system. It:
 *  1. Receives game events (job delivered, level-up, city visited, etc.)
 *  2. Writes those events to a JSON file the Python client reads.
 *  3. Reads the pending-grant JSON the Python client writes.
 *  4. Applies XP and money grants DIRECTLY to live game memory using
 *     pointers captured via hardware debug registers (DR0-DR2) + VEH.
 *     No save-file patching or F9 reload needed.
 *
 * Memory grant system
 * -------------------
 * Your friend identified three stable instruction addresses in amtrucks.exe:
 *
 *   0x7FF65D97DA69  mov [rdi+0x10], rcx   — money increase
 *   0x7FF65D62FE66  mov [rsi+0x62C], edi  — XP write
 *   0x7FF65D62A9DF  mov [rbx+0x10], rcx   — visited-city count write
 *
 * At plugin init we set hardware execution breakpoints (DR0-DR2) on those
 * addresses across all existing threads, and in DllMain / DLL_THREAD_ATTACH
 * for any threads created later.  A Vectored Exception Handler fires on the
 * first hit of each instruction, captures the relevant base-object register,
 * then disables that breakpoint.  Thereafter we read/write:
 *
 *   money  : *(int64_t*)(g_money_ptr + 0x10)
 *   XP     : *(int32_t*)(g_xp_ptr   + 0x62C)
 *   city count: *(int64_t*)(g_city_ptr + 0x10)  (polled for changes)
 *
 * Build requirements:
 *  - Windows x64
 *  - SCS SDK headers (https://modding.scssoft.com/wiki/SDK)
 *  - C++17 or later
 *  - nlohmann/json (single-header, /vendor/nlohmann/json.hpp)
 */

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <shlobj.h>
#include <tlhelp32.h>
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <set>
#include <map>
#include <mutex>
#include <atomic>
#include <thread>
#include <chrono>
#include <ctime>
#include <sstream>
#include <iomanip>

#include "scssdk_telemetry.h"
#include "eurotrucks2/scssdk_eut2.h"
#include "eurotrucks2/scssdk_telemetry_eut2.h"
#include "amtrucks/scssdk_ats.h"
#include "amtrucks/scssdk_telemetry_ats.h"

#include "nlohmann/json.hpp"

using json = nlohmann::json;
namespace fs = std::filesystem;

// ── Plugin version ─────────────────────────────────────────────────────────────
static const char* PLUGIN_VERSION = "2.22.0";

// ── Communication file paths ───────────────────────────────────────────────────
static fs::path g_comm_dir;
static fs::path g_events_file;
static fs::path g_items_file;
static fs::path g_applied_file;

// ── SCS logging ────────────────────────────────────────────────────────────────
static scs_log_t g_log = nullptr;

inline void log(const std::string& msg, scs_log_type_t level = SCS_LOG_TYPE_message) {
    if (g_log) g_log(level, ("[ATS-AP] " + msg).c_str());
}

// ── Shared game state ──────────────────────────────────────────────────────────
static std::mutex g_state_mutex;

struct GameState {
    bool      plugin_alive    = true;
    int       current_level   = 0;
    long long current_money   = 0;
    float     truck_x = 0, truck_y = 0, truck_z = 0;
    std::string current_cargo_id;
    std::string current_cargo_name;
    std::string current_source_city_id;   // set from job config; used for live arrival hint
    std::string current_dest_city_id;     // set from job config; used for live arrival hint
    bool      job_active       = false;
    bool      cargo_loaded     = false;   // false until SCS confirms cargo is on the trailer
    bool      in_game          = false;
    bool      city_count_changed = false;
    bool      dest_hint_sent   = false;   // true once nav-distance or delivery hint fires; reset per job
};

// ── Game identity (from SDK init params) ──────────────────────────────────────
// Reported in events.json so the client can tell the player which game build is
// running — the first thing to check when memory addresses stop verifying.
static std::string g_game_id;           // "ats" / "eut2"
static std::string g_game_version_str;  // "1.19" style, from SCS_GET_*_VERSION

// ── DeathLink state (engine wear telemetry) ────────────────────────────────────
// A death is sent when engine wear crosses DEATH_WEAR_THRESHOLD while driving.
// The latch prevents repeat deaths from one wreck; it re-arms after the player
// repairs below DEATH_REARM_THRESHOLD.  g_wear_baselined suppresses a spurious
// death when a save that already has a destroyed engine is loaded (the first
// wear sample after telemetry (re)starts only sets the latch, never fires).
static const float DEATH_WEAR_THRESHOLD  = 0.90f;
static const float DEATH_REARM_THRESHOLD = 0.60f;
static std::atomic<float> g_engine_wear{0.0f};
static bool   g_death_latched   = false;
static bool   g_wear_baselined  = false;
static double g_telemetry_started_at = 0.0;  // set on telemetry_started
static int    g_death_counter   = 0;

static GameState g_state;

// ── Item state (read from items.json) ─────────────────────────────────────────
struct ItemState {
    long long total_money_granted  = 0;
    long long total_money_deducted = 0;
    int       total_xp_granted     = 0;
    int       win_condition        = 0;
    int       goal_level           = 35;
    long long goal_money           = 1000000;
    double    last_read_time       = 0.0;
    std::string seed;  // AP seed name; used to detect new-seed transitions
    // Authoritative money/XP the client parsed from the save file.  Ground
    // truth for the value scanner that self-finds the live addresses when the
    // hard-coded instruction addresses no longer verify (game update).
    long long save_money           = -1;   // -1 = not provided
    int       save_xp              = -1;
};

static ItemState g_items;

// ── Event queue ────────────────────────────────────────────────────────────────
struct GameEvent {
    std::string id;
    std::string type;
    std::string game_id;
    json        extra;
};

static std::vector<GameEvent> g_event_queue;
static std::set<std::string>  g_sent_event_ids;

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
    if (SUCCEEDED(SHGetFolderPathW(nullptr, CSIDL_PERSONAL, nullptr, SHGFP_TYPE_CURRENT, path)))
        return fs::path(path);
    return fs::path(getenv("USERPROFILE")) / "Documents";
}

static std::string hex_addr(uintptr_t addr) {
    std::ostringstream oss;
    oss << "0x" << std::hex << std::uppercase << addr;
    return oss.str();
}

// ── XP → level table (mirrors ATSClient.py) ───────────────────────────────────
static const int XP_THRESHOLDS[] = {
    0, 0, 500, 1200, 2100, 3200, 4500, 6000, 7700, 9600,
    11700, 14000, 16500, 19200, 22100, 25200, 28500, 32000,
    35700, 39600, 43700, 48000, 52500, 57200, 62100, 67200,
    72500, 78000, 83700, 89600, 95700, 102000, 108500, 115200,
    122100, 129200, 136500, 144000, 151700, 159600, 167700,
};
static const int XP_LEVELS = (int)(sizeof(XP_THRESHOLDS) / sizeof(XP_THRESHOLDS[0])) - 1;

static int xp_to_level(int xp) {
    for (int lvl = XP_LEVELS; lvl >= 1; --lvl)
        if (xp >= XP_THRESHOLDS[lvl]) return lvl;
    return 1;
}

// ── Memory grant system ────────────────────────────────────────────────────────
//
// Instruction addresses are resolved at runtime from the amtrucks.exe module
// base + a pre-computed RVA.  If the bytes at the RVA-derived address don't
// match (different game version), XP falls back to an AOB scan.  All three
// addresses are logged at startup so mismatches are immediately diagnosable.
//
// Friend's absolute addresses (confirmed stable on their machine):
//   0x7FF65D97DA69  mov [rdi+0x10],  rcx  — money write
//   0x7FF65D62FE66  mov [rsi+0x62C], edi  — XP write
//   0x7FF65D62A9DF  mov [rbx+0x10],  rcx  — city-count write
//
// Assumed friend base: 0x7FF65D000000  → RVAs below.

static const uintptr_t FRIEND_BASE = 0x7FF65D210000ULL;
static const uintptr_t MONEY_RVA   = 0x7FF65D97DA69ULL - FRIEND_BASE; // 0x76DA69
static const uintptr_t XP_RVA      = 0x7FF65D62FE66ULL - FRIEND_BASE; // 0x41FE66
static const uintptr_t CITY_RVA    = 0x7FF65D62A9DFULL - FRIEND_BASE; // 0x41A9DF

// Expected machine-code bytes at each instruction (for verification + AOB).
//   48 89 4F 10        mov [rdi+0x10],  rcx
//   89 BE 2C 06 00 00  mov [rsi+0x62C], edi   ← 6 unique bytes → AOB-scannable
//   48 89 4B 10        mov [rbx+0x10],  rcx
static const uint8_t MONEY_PATTERN[] = {0x48, 0x89, 0x4F, 0x10};
static const uint8_t XP_PATTERN[]    = {0x89, 0xBE, 0x2C, 0x06, 0x00, 0x00};
static const uint8_t CITY_PATTERN[]  = {0x48, 0x89, 0x4B, 0x10};

// Resolved at init — 0 means not found; breakpoint + capture disabled.
static uintptr_t ADDR_MONEY_INC  = 0;
static uintptr_t ADDR_XP_WRITE   = 0;
static uintptr_t ADDR_CITY_COUNT = 0;

static const ptrdiff_t MONEY_OFFSET    = 0x10;
static const ptrdiff_t XP_OFFSET       = 0x62C;
static const ptrdiff_t CITY_CNT_OFFSET = 0x10;

// Base object pointers — captured once by VEH, then used for direct R/W.
static std::atomic<uintptr_t> g_money_ptr{0};
static std::atomic<uintptr_t> g_xp_ptr{0};
static std::atomic<uintptr_t> g_city_ptr{0};

// Cumulative grant amounts written to the player's save across all sessions.
// Loaded from applied.json on startup so restarts don't re-apply old grants.
static long long g_applied_money  = 0;
static int       g_applied_xp     = 0;
// When a fine exceeds the current balance (clamped to $0), the uncollected
// portion is stored here.  Future grants pay off this debt first so that
// delivery income isn't permanently lost to the clamp.
static long long g_clamped_debt   = 0;
static std::atomic<bool> g_applied_dirty{false};

// Seed identifier written by the AP client into items.json.
// When this changes, applied counters are reset so new-seed grants start from zero.
static std::string g_current_seed;

// Active ATS profile ID parsed from config.cfg (hex string, e.g. "4D696C6573").
// Written to events.json so the client can restrict save-file discovery to the
// correct profile and never fall back to reading a different profile's save.
static std::string g_active_profile_id;

// Read the active profile ID from config.cfg and update g_active_profile_id.
// Falls back to a profile-directory mtime scan if config.cfg has no usable key.
// Called at init, telemetry_started, and telemetry_paused.
static void refresh_active_profile_id() {
    fs::path docs = get_documents_path() / "American Truck Simulator";
    fs::path cfg  = docs / "config.cfg";

    // Suppress the attempt log after the ID is resolved — it fires on every
    // telemetry_started/paused and would spam the log when already known.
    if (g_active_profile_id.empty())
        log("config.cfg read attempt: " + cfg.string());

    // Known cvar names for the active profile in ATS/ETS2 config.cfg.
    // Tried longest-first so a shorter key can't shadow a longer one.
    static const char* const PROFILE_KEYS[] = {
        "g_last_select_profile_id",
        "g_last_select_profile",
        "g_profile",
        nullptr
    };

    // Try config.cfg first.  Returns true when the profile ID is resolved.
    auto try_cfg = [&]() -> bool {
        try {
            std::ifstream f(cfg);
            if (!f.is_open()) {
                if (g_active_profile_id.empty())
                    log("config.cfg: could not open — will try directory scan");
                return false;
            }
            std::string line;
            while (std::getline(f, line)) {
                for (int k = 0; PROFILE_KEYS[k]; ++k) {
                    size_t pos = line.find(PROFILE_KEYS[k]);
                    if (pos == std::string::npos) continue;
                    // Word-boundary guard: next char must not be alphanumeric or '_'.
                    size_t after = pos + strlen(PROFILE_KEYS[k]);
                    if (after < line.size() &&
                        (isalnum((unsigned char)line[after]) || line[after] == '_'))
                        continue;
                    size_t q1 = line.find('"', after);
                    if (q1 == std::string::npos) continue;
                    size_t q2 = line.find('"', q1 + 1);
                    if (q2 == std::string::npos) continue;
                    std::string pid = line.substr(q1 + 1, q2 - q1 - 1);
                    if (pid.empty()) {
                        if (g_active_profile_id.empty())
                            log(std::string("config.cfg: key '") + PROFILE_KEYS[k] +
                                "' found but value is empty — will try directory scan");
                        return false;
                    }
                    if (pid != g_active_profile_id) {
                        g_active_profile_id = pid;
                        log("config.cfg active profile resolved: " + g_active_profile_id +
                            " (key=" + PROFILE_KEYS[k] + ")");
                    }
                    return true;
                }
            }
            if (g_active_profile_id.empty())
                log("config.cfg: no profile key found "
                    "(tried g_last_select_profile_id / g_last_select_profile / g_profile)"
                    " — will try directory scan");
            return false;
        } catch (...) {
            log("config.cfg: exception while reading — will try directory scan");
            return false;
        }
    };

    if (try_cfg()) return;

    // Fallback: scan for the most recently modified profile directory in Documents.
    // When ATS loads a profile it writes to that profile's directory (saves, metadata).
    // A brand-new profile directory was just created and will have the newest mtime.
    const fs::path scan_roots[] = {
        docs / "profiles",
        docs / "steam" / "profiles",
    };

    std::string newest_id;
    fs::file_time_type newest_time;
    bool have_newest = false;

    for (const auto& root : scan_roots) {
        try {
            if (!fs::exists(root)) continue;
            for (const auto& entry : fs::directory_iterator(root)) {
                if (!entry.is_directory()) continue;
                auto mtime = entry.last_write_time();
                if (!have_newest || mtime > newest_time) {
                    newest_time = mtime;
                    newest_id   = entry.path().filename().string();
                    have_newest = true;
                }
            }
        } catch (...) {}
    }

    if (!newest_id.empty()) {
        if (newest_id != g_active_profile_id) {
            g_active_profile_id = newest_id;
            log("Active profile from directory scan (newest mtime): " + g_active_profile_id);
        }
    } else if (g_active_profile_id.empty()) {
        log("Directory scan: no profiles found in " + (docs / "profiles").string());
    }
}

// Previous city count — detects increases without keeping the breakpoint live.
static uint64_t g_prev_city_count = UINT64_MAX; // UINT64_MAX = uninitialized

static std::mutex              g_apply_mutex;          // prevents concurrent double-apply

// Re-entrancy suppression: true while apply_memory_grants is writing to memory.
// telemetry_paused / telemetry_started skip calling apply_memory_grants while this is set
// so that the game's own save-triggered pause/resume cycle can't re-enter the write path.
static std::atomic<bool> g_applying_grant{false};

// Last values we wrote to memory.  Used to skip a redundant write if the game
// hasn't changed the value since our last write (delta would produce the same address value).
static long long g_last_written_money = LLONG_MIN;
static int32_t   g_last_written_xp    = INT32_MIN;

// Per-delivery idempotency guard: records which cargo delivery last triggered a grant
// application.  Prevents duplicate job_delivered events from applying the same grant twice.
static std::string g_applied_delivery_id;

static PVOID   g_veh_handle   = nullptr;
static HANDLE  g_apply_timer  = nullptr;  // paused-grant timer; see pause_grant_timer_cb

static bool all_ptrs_captured() {
    return g_money_ptr.load(std::memory_order_relaxed) != 0 &&
           g_xp_ptr.load(std::memory_order_relaxed)   != 0 &&
           g_city_ptr.load(std::memory_order_relaxed)  != 0;
}

// Safe memory helpers using ReadProcessMemory / WriteProcessMemory.
// These are standard Win32 APIs — unlike __try/__except (MSVC SEH) they compile
// cleanly with MinGW/GCC and return FALSE instead of crashing on bad addresses.
static HANDLE g_self = INVALID_HANDLE_VALUE;  // set in scs_telemetry_init

static int64_t safe_read_i64(uintptr_t addr) {
    int64_t v = 0;
    SIZE_T n = 0;
    if (!ReadProcessMemory(g_self, reinterpret_cast<LPCVOID>(addr), &v, sizeof(v), &n) || n != sizeof(v))
        log("safe_read_i64 failed at " + hex_addr(addr), SCS_LOG_TYPE_warning);
    return v;
}

static int32_t safe_read_i32(uintptr_t addr) {
    int32_t v = 0;
    SIZE_T n = 0;
    if (!ReadProcessMemory(g_self, reinterpret_cast<LPCVOID>(addr), &v, sizeof(v), &n) || n != sizeof(v))
        log("safe_read_i32 failed at " + hex_addr(addr), SCS_LOG_TYPE_warning);
    return v;
}

static bool safe_write_i64(uintptr_t addr, int64_t val) {
    SIZE_T n = 0;
    if (!WriteProcessMemory(g_self, reinterpret_cast<LPVOID>(addr), &val, sizeof(val), &n) || n != sizeof(val)) {
        log("safe_write_i64 failed at " + hex_addr(addr), SCS_LOG_TYPE_warning);
        return false;
    }
    return true;
}

static bool safe_write_i32(uintptr_t addr, int32_t val) {
    SIZE_T n = 0;
    if (!WriteProcessMemory(g_self, reinterpret_cast<LPVOID>(addr), &val, sizeof(val), &n) || n != sizeof(val)) {
        log("safe_write_i32 failed at " + hex_addr(addr), SCS_LOG_TYPE_warning);
        return false;
    }
    return true;
}

// Quiet u64 read for speculative probing — returns false instead of logging,
// because city-token probing intentionally reads addresses that may be invalid.
static bool read_u64_quiet(uintptr_t addr, uint64_t* out) {
    SIZE_T n = 0;
    return ReadProcessMemory(g_self, reinterpret_cast<LPCVOID>(addr),
                             out, sizeof(*out), &n) && n == sizeof(*out);
}

// ── SCS token decoding ─────────────────────────────────────────────────────────
// SCS engine "tokens" pack a short identifier (max 12 chars of [0-9a-z_]) into
// a u64 using base-38 encoding.  City IDs are stored as tokens in game memory;
// decoding them lets us name a newly-discovered city without any save-file read.

static std::string decode_scs_token(uint64_t token) {
    static const char CHARSET[] = "\0" "0123456789abcdefghijklmnopqrstuvwxyz_";
    std::string out;
    while (token) {
        uint64_t c = token % 38;
        token /= 38;
        if (c == 0 || c > 37) return std::string();  // invalid digit — not a token
        out.push_back(CHARSET[c]);
        if (out.size() > 12) return std::string();   // tokens are at most 12 chars
    }
    return out;
}

// A decoded string is plausibly a city token if it looks like an SCS city ID:
// starts with a letter, 2-12 chars of lowercase/digits/underscore.
static bool plausible_city_token(const std::string& s) {
    if (s.size() < 2 || s.size() > 12) return false;
    if (s[0] < 'a' || s[0] > 'z') return false;
    for (char c : s)
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_'))
            return false;
    return true;
}

// Probe the visited-city registry for the token of the most recently added city.
// The registry object (g_city_ptr) keeps its count at +0x10; the backing array
// pointer lives at a nearby offset that varies between game builds, so several
// candidate layouts are probed.  Elements are either inline u64 tokens or
// pointers to city objects with the token near the start.  All reads use
// ReadProcessMemory and cannot fault; implausible decodes are discarded.  The
// client validates every candidate against its authoritative city list, so a
// stray false positive here is filtered out before any check is sent.
static std::vector<std::string> probe_city_candidates(uintptr_t cp, uint64_t count) {
    std::vector<std::string> out;
    if (!cp || count == 0) return out;

    auto add = [&](uint64_t raw) {
        std::string t = decode_scs_token(raw);
        if (plausible_city_token(t) &&
            std::find(out.begin(), out.end(), t) == out.end() &&
            out.size() < 8)
            out.push_back(t);
    };
    auto looks_like_ptr = [](uint64_t v) {
        return v >= 0x10000ULL && v <= 0x00007FFFFFFFFFFFULL;
    };

    static const ptrdiff_t ARRAY_OFFSETS[] = {0x0, 0x8, 0x18};
    static const size_t    STRIDES[]       = {8, 16};

    for (ptrdiff_t arr_off : ARRAY_OFFSETS) {
        uint64_t arr = 0;
        if (!read_u64_quiet(cp + arr_off, &arr) || !looks_like_ptr(arr)) continue;
        for (size_t stride : STRIDES) {
            uint64_t elem = 0;
            if (!read_u64_quiet((uintptr_t)(arr + (count - 1) * stride), &elem)) continue;
            add(elem);                       // element is an inline token
            if (looks_like_ptr(elem)) {      // element is a pointer to a city object
                for (ptrdiff_t o : {0x0, 0x8, 0x10, 0x18}) {
                    uint64_t v = 0;
                    if (read_u64_quiet((uintptr_t)(elem + o), &v)) add(v);
                }
            }
        }
    }
    return out;
}

// ── Address resolution helpers ────────────────────────────────────────────────

static std::string bytes_hex(const uint8_t* buf, size_t len) {
    std::ostringstream oss;
    for (size_t i = 0; i < len; ++i) {
        if (i) oss << ' ';
        oss << std::hex << std::uppercase << std::setw(2) << std::setfill('0') << (int)buf[i];
    }
    return oss.str();
}

static bool verify_bytes_at(uintptr_t addr, const uint8_t* pattern, size_t patlen) {
    if (!addr) return false;
    uint8_t buf[16] = {};
    SIZE_T n = 0;
    if (!ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<LPCVOID>(addr),
                           buf, patlen, &n) || n != patlen)
        return false;
    return memcmp(buf, pattern, patlen) == 0;
}

// Scan [base, base+imgSize) for `pattern`, reading in 64 KB chunks with overlap.
static uintptr_t aob_scan(uintptr_t base, size_t imgSize,
                           const uint8_t* pattern, size_t patlen) {
    const size_t CHUNK = 65536;
    std::vector<uint8_t> buf(CHUNK + patlen - 1);

    for (size_t off = 0; off < imgSize; off += CHUNK) {
        size_t toRead = std::min(CHUNK + patlen - 1, imgSize - off);
        SIZE_T n = 0;
        if (!ReadProcessMemory(GetCurrentProcess(),
                               reinterpret_cast<LPCVOID>(base + off),
                               buf.data(), toRead, &n) || n < patlen)
            continue;
        for (size_t i = 0; i + patlen <= n; ++i) {
            if (memcmp(buf.data() + i, pattern, patlen) == 0)
                return base + off + i;
        }
    }
    return 0;
}

// Get PE SizeOfImage without psapi — read the PE header directly.
static size_t pe_image_size(uintptr_t base) {
    uint8_t hdr[1024] = {};
    SIZE_T n = 0;
    if (!ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<LPCVOID>(base),
                           hdr, sizeof(hdr), &n) || n < sizeof(IMAGE_DOS_HEADER))
        return 0;
    auto* dos = reinterpret_cast<IMAGE_DOS_HEADER*>(hdr);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    LONG lfanew = dos->e_lfanew;
    if (lfanew < 0 || (size_t)lfanew + sizeof(IMAGE_NT_HEADERS64) > n) return 0;
    auto* nt = reinterpret_cast<IMAGE_NT_HEADERS64*>(hdr + lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    return nt->OptionalHeader.SizeOfImage;
}

// Optional RVA overrides from <comm_dir>/addresses.json.  When a game update
// moves the target instructions, a maintainer can publish new RVAs in this
// file and players fix their install WITHOUT waiting for a recompiled DLL:
//   { "money_rva": "0x76DA69", "xp_rva": "0x41FE66", "city_rva": "0x41A9DF" }
// Values are hex strings (with or without 0x) or plain numbers.
static void load_rva_overrides(uintptr_t& money_rva, uintptr_t& xp_rva,
                               uintptr_t& city_rva) {
    fs::path f = g_comm_dir / "addresses.json";
    if (!fs::exists(f)) return;
    try {
        std::ifstream in(f);
        json j = json::parse(in);
        auto get = [&](const char* key, uintptr_t& dst) {
            if (!j.contains(key)) return;
            uintptr_t v = 0;
            if (j[key].is_string())
                v = (uintptr_t)std::stoull(j[key].get<std::string>(), nullptr, 16);
            else if (j[key].is_number_unsigned())
                v = (uintptr_t)j[key].get<uint64_t>();
            if (v) {
                dst = v;
                log(std::string("addresses.json override: ") + key + " = " + hex_addr(v));
            }
        };
        get("money_rva", money_rva);
        get("xp_rva",    xp_rva);
        get("city_rva",  city_rva);
    } catch (const std::exception& e) {
        log(std::string("addresses.json parse failed (ignored): ") + e.what(),
            SCS_LOG_TYPE_warning);
    }
}

// ── Automatic value scanner (Cheat-Engine-style, no human required) ────────────
//
// When the hard-coded instruction addresses no longer verify after a game
// update, the breakpoint path can never capture the money/XP pointers.  This
// scanner recovers them automatically:
//
//   1. The client parses the save and sends the exact money/XP values.
//   2. We scan the game's writable memory for the money value → candidate
//      addresses (an "exact value" scan).
//   3. When the player's money changes (delivery, fuel, toll), the client
//      sends the new value; we keep only candidates that tracked the change
//      (a "next scan").  An address that follows a REAL change is provably the
//      live economy value — not a stale "last saved" cache.
//   4. The confirmed address is stored in the SAME pointer variable the
//      breakpoint path fills (as addr - OFFSET), so injection, stale-detection
//      and flushing all work unchanged and live grants resume instantly.
//
// Everything here uses ReadProcessMemory and cannot fault.  Writes only ever
// happen later, through the existing apply path, after confirmation.

struct ScanState {
    std::vector<uintptr_t> money_addrs;
    std::vector<uintptr_t> xp_addrs;
    long long money_last = LLONG_MIN;  // value the money candidate set was last filtered on
    int       xp_last    = INT32_MIN;
    bool money_scanned = false;
    bool xp_scanned    = false;
    int  money_confirms = 0;   // number of real value changes the survivors tracked
    int  xp_confirms    = 0;
};
static ScanState g_scan;
static std::mutex g_scan_state_mutex;
static std::atomic<bool> g_scanning{false};
static std::atomic<bool> g_money_via_scan{false};
static std::atomic<bool> g_xp_via_scan{false};

// A value common enough to exceed this many hits is too ambiguous to be useful
// on its own; we discard the scan and wait for the value to change to something
// rarer.  (Truncating would risk dropping the true address.)
static const size_t SCAN_MAX_HITS = 40000;

// Scan committed, writable, private memory for an exact 8-byte value at 4-byte
// alignment.  Sets *capped=true (and returns partial) if SCAN_MAX_HITS reached.
template <typename T>
static std::vector<uintptr_t> scan_process_for_value(T value, bool* capped) {
    std::vector<uintptr_t> hits;
    *capped = false;
    HANDLE proc = GetCurrentProcess();
    MEMORY_BASIC_INFORMATION mbi;
    uintptr_t addr = 0;
    const size_t CHUNK = 4 * 1024 * 1024;
    std::vector<uint8_t> buf(CHUNK);

    while (VirtualQueryEx(proc, (LPCVOID)addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        uintptr_t region_base = (uintptr_t)mbi.BaseAddress;
        size_t    region_size = mbi.RegionSize;
        uintptr_t next = region_base + region_size;
        if (next <= addr) break;  // no forward progress — stop
        addr = next;

        if (mbi.State != MEM_COMMIT) continue;
        DWORD prot = mbi.Protect;
        if (prot & PAGE_GUARD) continue;
        if (!(prot & (PAGE_READWRITE | PAGE_WRITECOPY |
                      PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY))) continue;

        for (size_t off = 0; off < region_size; off += CHUNK) {
            size_t toRead = (std::min)(CHUNK, region_size - off);
            SIZE_T n = 0;
            if (!ReadProcessMemory(proc, (LPCVOID)(region_base + off),
                                   buf.data(), toRead, &n) || n < sizeof(T))
                continue;
            for (size_t i = 0; i + sizeof(T) <= n; i += 4) {
                T v;
                memcpy(&v, buf.data() + i, sizeof(T));
                if (v == value) {
                    hits.push_back(region_base + off + i);
                    if (hits.size() >= SCAN_MAX_HITS) { *capped = true; return hits; }
                }
            }
        }
    }
    return hits;
}

// Re-read every candidate; keep only those still holding `value` ("next scan").
template <typename T>
static void keep_candidates_matching(std::vector<uintptr_t>& addrs, T value) {
    HANDLE proc = GetCurrentProcess();
    std::vector<uintptr_t> keep;
    keep.reserve(addrs.size());
    for (uintptr_t a : addrs) {
        T v; SIZE_T n = 0;
        if (ReadProcessMemory(proc, (LPCVOID)a, &v, sizeof(T), &n) &&
            n == sizeof(T) && v == value)
            keep.push_back(a);
    }
    addrs.swap(keep);
}

// One scan/refine pass.  Runs on a detached worker thread so a multi-hundred-MB
// scan never stalls the game frame.  Locks in an address only after a survivor
// has tracked at least one real value change (proves it is live economy state).
static void value_scan_pass() {
    long long km; int kx;
    bool plugin_alive;
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        km = g_items.save_money;
        kx = g_items.save_xp;
        plugin_alive = g_state.plugin_alive;
    }
    if (!plugin_alive) return;

    // Only scan for a field whose breakpoint pointer is missing AND whose
    // instruction address failed to resolve (the update-broke-it case).
    bool need_money = (g_money_ptr.load(std::memory_order_relaxed) == 0 &&
                       ADDR_MONEY_INC == 0 && km > 1000);
    bool need_xp    = (g_xp_ptr.load(std::memory_order_relaxed) == 0 &&
                       ADDR_XP_WRITE == 0 && kx > 100);
    if (!need_money && !need_xp) return;

    std::lock_guard<std::mutex> scan_lock(g_scan_state_mutex);

    if (need_money) {
        bool capped = false;
        if (!g_scan.money_scanned) {
            g_scan.money_addrs = scan_process_for_value<long long>(km, &capped);
            if (capped) {
                log("Value scan (money): $" + std::to_string(km) +
                    " too common (>" + std::to_string(SCAN_MAX_HITS) +
                    " hits) — waiting for a rarer balance", SCS_LOG_TYPE_message);
                g_scan.money_addrs.clear();
            } else {
                g_scan.money_scanned = true;
                g_scan.money_last = km;
                log("Value scan (money): first pass found " +
                    std::to_string(g_scan.money_addrs.size()) +
                    " candidate(s) for $" + std::to_string(km));
            }
        } else if (km != g_scan.money_last) {
            keep_candidates_matching<long long>(g_scan.money_addrs, km);
            g_scan.money_last = km;
            g_scan.money_confirms++;
            log("Value scan (money): after change to $" + std::to_string(km) +
                ", " + std::to_string(g_scan.money_addrs.size()) +
                " candidate(s) remain (confirms=" +
                std::to_string(g_scan.money_confirms) + ")");
            if (g_scan.money_addrs.empty()) {
                // All pruned — the value we scanned was stale. Start over.
                g_scan.money_scanned = false;
                g_scan.money_confirms = 0;
            }
        }
        // Lock in once a single survivor has tracked at least one real change.
        if (g_scan.money_addrs.size() == 1 && g_scan.money_confirms >= 1) {
            uintptr_t a = g_scan.money_addrs[0];
            g_money_ptr.store(a - MONEY_OFFSET, std::memory_order_relaxed);
            g_money_via_scan.store(true, std::memory_order_relaxed);
            log("Money address FOUND via value scan @ " + hex_addr(a) +
                " — live money grants restored (no addresses.json needed)");
        }
    }

    if (need_xp) {
        bool capped = false;
        if (!g_scan.xp_scanned) {
            g_scan.xp_addrs = scan_process_for_value<int32_t>((int32_t)kx, &capped);
            if (capped) {
                g_scan.xp_addrs.clear();  // too common; wait for a rarer XP value
            } else {
                g_scan.xp_scanned = true;
                g_scan.xp_last = kx;
                log("Value scan (XP): first pass found " +
                    std::to_string(g_scan.xp_addrs.size()) +
                    " candidate(s) for " + std::to_string(kx) + " XP");
            }
        } else if (kx != g_scan.xp_last) {
            keep_candidates_matching<int32_t>(g_scan.xp_addrs, (int32_t)kx);
            g_scan.xp_last = kx;
            g_scan.xp_confirms++;
            if (g_scan.xp_addrs.empty()) {
                g_scan.xp_scanned = false;
                g_scan.xp_confirms = 0;
            }
        }
        // XP int32 has more false matches, so require two confirmed changes.
        if (g_scan.xp_addrs.size() == 1 && g_scan.xp_confirms >= 2) {
            uintptr_t a = g_scan.xp_addrs[0];
            g_xp_ptr.store(a - XP_OFFSET, std::memory_order_relaxed);
            g_xp_via_scan.store(true, std::memory_order_relaxed);
            log("XP address FOUND via value scan @ " + hex_addr(a) +
                " — live XP grants restored");
        }
    }
}

// Discard scanner progress so the next pass starts from a fresh full scan.
// Called when a scanned address goes stale (object moved) and when a new save
// loads (telemetry_started) — in both cases the old candidate list is invalid.
static void reset_money_scan() {
    std::lock_guard<std::mutex> lock(g_scan_state_mutex);
    g_scan.money_addrs.clear();
    g_scan.money_scanned = false;
    g_scan.money_confirms = 0;
    g_scan.money_last = LLONG_MIN;
    g_money_via_scan.store(false, std::memory_order_relaxed);
}
static void reset_xp_scan() {
    std::lock_guard<std::mutex> lock(g_scan_state_mutex);
    g_scan.xp_addrs.clear();
    g_scan.xp_scanned = false;
    g_scan.xp_confirms = 0;
    g_scan.xp_last = INT32_MIN;
    g_xp_via_scan.store(false, std::memory_order_relaxed);
}

// Kick off a scan pass on a worker thread if one isn't already running and a
// field still needs finding.  Called from the frame poll.
static void maybe_start_value_scan() {
    bool need_money = (g_money_ptr.load(std::memory_order_relaxed) == 0 && ADDR_MONEY_INC == 0);
    bool need_xp    = (g_xp_ptr.load(std::memory_order_relaxed)   == 0 && ADDR_XP_WRITE   == 0);
    if (!need_money && !need_xp) return;
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        if (g_items.save_money <= 1000 && g_items.save_xp <= 100) return;  // nothing usable yet
    }
    if (g_scanning.exchange(true)) return;  // a pass is already in flight
    std::thread([]() {
        try { value_scan_pass(); } catch (...) {}
        g_scanning.store(false, std::memory_order_release);
    }).detach();
}

// Called once at plugin init.  Fills ADDR_MONEY_INC / ADDR_XP_WRITE / ADDR_CITY_COUNT.
static void resolve_addresses() {
    HMODULE hMod = GetModuleHandleA("amtrucks.exe");
    if (!hMod) {
        log("ADDR resolve ERROR: GetModuleHandle(amtrucks.exe) returned NULL",
            SCS_LOG_TYPE_error);
        return;
    }

    uintptr_t base    = reinterpret_cast<uintptr_t>(hMod);
    size_t    imgSize = pe_image_size(base);
    log("amtrucks.exe base=" + hex_addr(base) +
        " size=" + std::to_string(imgSize / 1024) + " KB  "
        "(friend_base=" + hex_addr(FRIEND_BASE) + ")");

    uintptr_t money_rva = MONEY_RVA;
    uintptr_t xp_rva    = XP_RVA;
    uintptr_t city_rva  = CITY_RVA;
    load_rva_overrides(money_rva, xp_rva, city_rva);

    // ── Money ──────────────────────────────────────────────────────────────────
    {
        uintptr_t addr = base + money_rva;
        uint8_t   found[8] = {};
        SIZE_T    n = 0;
        ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<LPCVOID>(addr),
                          found, sizeof(MONEY_PATTERN), &n);
        if (verify_bytes_at(addr, MONEY_PATTERN, sizeof(MONEY_PATTERN))) {
            ADDR_MONEY_INC = addr;
            log("ADDR money   OK  @ " + hex_addr(addr) +
                "  bytes=" + bytes_hex(found, sizeof(MONEY_PATTERN)));
        } else {
            log("ADDR money   FAIL@ " + hex_addr(addr) +
                "  found=" + bytes_hex(found, n) +
                "  want=" + bytes_hex(MONEY_PATTERN, sizeof(MONEY_PATTERN)) +
                "  (game update likely moved this instruction — live money grants "
                "disabled; the AP client will deliver grants via the save file. "
                "To restore live grants, put the new RVA in archipelago/addresses.json)",
                SCS_LOG_TYPE_warning);
        }
    }

    // ── XP — try RVA first, then AOB scan (6-byte pattern is fairly unique) ──
    {
        uintptr_t addr = base + xp_rva;
        uint8_t   found[8] = {};
        SIZE_T    n = 0;
        ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<LPCVOID>(addr),
                          found, sizeof(XP_PATTERN), &n);
        if (verify_bytes_at(addr, XP_PATTERN, sizeof(XP_PATTERN))) {
            ADDR_XP_WRITE = addr;
            log("ADDR xp      OK  @ " + hex_addr(addr) +
                "  bytes=" + bytes_hex(found, sizeof(XP_PATTERN)));
        } else {
            log("ADDR xp      FAIL@ " + hex_addr(addr) +
                "  found=" + bytes_hex(found, n) +
                "  want=" + bytes_hex(XP_PATTERN, sizeof(XP_PATTERN)) +
                "  — trying AOB scan...",
                SCS_LOG_TYPE_warning);
            if (imgSize > 0) {
                uintptr_t hit = aob_scan(base, imgSize, XP_PATTERN, sizeof(XP_PATTERN));
                if (hit) {
                    ADDR_XP_WRITE = hit;
                    log("ADDR xp      AOB @ " + hex_addr(hit) + "  (XP grants enabled)");
                } else {
                    log("ADDR xp      AOB found no match — XP grants disabled",
                        SCS_LOG_TYPE_warning);
                }
            }
        }
    }

    // ── City count ─────────────────────────────────────────────────────────────
    {
        uintptr_t addr = base + city_rva;
        uint8_t   found[8] = {};
        SIZE_T    n = 0;
        ReadProcessMemory(GetCurrentProcess(), reinterpret_cast<LPCVOID>(addr),
                          found, sizeof(CITY_PATTERN), &n);
        if (verify_bytes_at(addr, CITY_PATTERN, sizeof(CITY_PATTERN))) {
            ADDR_CITY_COUNT = addr;
            log("ADDR city    OK  @ " + hex_addr(addr) +
                "  bytes=" + bytes_hex(found, sizeof(CITY_PATTERN)));
        } else {
            log("ADDR city    FAIL@ " + hex_addr(addr) +
                "  found=" + bytes_hex(found, n) +
                "  want=" + bytes_hex(CITY_PATTERN, sizeof(CITY_PATTERN)) +
                "  (game update likely moved this instruction — live city detection "
                "disabled; city checks fall back to save-file polling. "
                "To restore, put the new RVA in archipelago/addresses.json)",
                SCS_LOG_TYPE_warning);
        }
    }
}

// ── Hardware breakpoint helpers ────────────────────────────────────────────────
// DR7 layout: bit 0 = L0 (enable DR0), bit 2 = L1, bit 4 = L2.
// Condition/size fields default to 0 (execution breakpoint, correct).

static void set_bp_on_thread(HANDLE thread) {
    // Skip entirely if no addresses were resolved (avoids spurious single-steps).
    if (!ADDR_MONEY_INC && !ADDR_XP_WRITE && !ADDR_CITY_COUNT) return;

    CONTEXT ctx = {};
    ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(thread, &ctx)) return;

    // Only arm DRs for pointers that haven't been captured yet (or were reset
    // after stale detection).  This prevents re-arming already-valid pointers,
    // and means a re-arm after stale reset only breaks on the reset addresses.
    ctx.Dr7 = 0;
    ctx.Dr0 = ctx.Dr1 = ctx.Dr2 = ctx.Dr3 = 0;
    if (ADDR_MONEY_INC  && g_money_ptr.load(std::memory_order_relaxed) == 0)
        { ctx.Dr0 = ADDR_MONEY_INC;  ctx.Dr7 |= (1ULL << 0); }
    if (ADDR_XP_WRITE   && g_xp_ptr.load(std::memory_order_relaxed)    == 0)
        { ctx.Dr1 = ADDR_XP_WRITE;   ctx.Dr7 |= (1ULL << 2); }
    if (ADDR_CITY_COUNT && g_city_ptr.load(std::memory_order_relaxed)   == 0)
        { ctx.Dr2 = ADDR_CITY_COUNT;  ctx.Dr7 |= (1ULL << 4); }
    SetThreadContext(thread, &ctx);
}

static void clear_bp_on_thread(HANDLE thread) {
    CONTEXT ctx = {};
    ctx.ContextFlags = CONTEXT_DEBUG_REGISTERS;
    if (!GetThreadContext(thread, &ctx)) return;
    ctx.Dr0 = ctx.Dr1 = ctx.Dr2 = ctx.Dr3 = ctx.Dr7 = 0;
    SetThreadContext(thread, &ctx);
}

static void set_bp_all_threads() {
    DWORD pid  = GetCurrentProcessId();
    DWORD self = GetCurrentThreadId();

    // Set on current thread directly (no open/suspend needed).
    set_bp_on_thread(GetCurrentThread());

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return;

    THREADENTRY32 te = { sizeof(THREADENTRY32) };
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != pid || te.th32ThreadID == self) continue;
            HANDLE th = OpenThread(
                THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_SUSPEND_RESUME,
                FALSE, te.th32ThreadID);
            if (!th) continue;
            SuspendThread(th);
            set_bp_on_thread(th);
            ResumeThread(th);
            CloseHandle(th);
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
}

static void clear_bp_all_threads() {
    DWORD pid  = GetCurrentProcessId();
    DWORD self = GetCurrentThreadId();

    clear_bp_on_thread(GetCurrentThread());

    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) return;

    THREADENTRY32 te = { sizeof(THREADENTRY32) };
    if (Thread32First(snap, &te)) {
        do {
            if (te.th32OwnerProcessID != pid || te.th32ThreadID == self) continue;
            HANDLE th = OpenThread(
                THREAD_GET_CONTEXT | THREAD_SET_CONTEXT | THREAD_SUSPEND_RESUME,
                FALSE, te.th32ThreadID);
            if (!th) continue;
            SuspendThread(th);
            clear_bp_on_thread(th);
            ResumeThread(th);
            CloseHandle(th);
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
}

// ── Vectored Exception Handler ─────────────────────────────────────────────────
static LONG WINAPI ats_veh(EXCEPTION_POINTERS* ep) {
    if (ep->ExceptionRecord->ExceptionCode != EXCEPTION_SINGLE_STEP)
        return EXCEPTION_CONTINUE_SEARCH;

    CONTEXT*  ctx = ep->ContextRecord;
    uintptr_t ip  = ctx->Rip;

    // IMPORTANT: always clear the DR for this thread when the breakpoint fires,
    // regardless of whether we capture.  Without this, a thread that hits the
    // instruction after the pointer was already captured (or re-armed on another
    // thread first) would keep generating EXCEPTION_SINGLE_STEP every hit.
    if (ADDR_MONEY_INC && ip == ADDR_MONEY_INC) {
        if (g_money_ptr.load(std::memory_order_relaxed) == 0) {
            g_money_ptr.store(ctx->Rdi, std::memory_order_relaxed);
            log("Memory: money pointer captured " + hex_addr(ctx->Rdi));
            // The instruction (MOV [RDI+0x10], RCX) hasn't executed yet; RCX holds
            // the new balance the game is about to write.  Inject our net grant delta
            // into RCX now so the game's own instruction commits the combined value.
            // net_money = granted - deducted (traps); delta can be negative (fine).
            long long net_money = g_items.total_money_granted - g_items.total_money_deducted;
            long long delta = net_money - (long long)g_applied_money;
            if (delta != 0) {
                // Grants: pay off outstanding clamp-debt first so delivery income is
                // not permanently lost to a fine that exceeded the previous balance.
                long long effective_delta = delta;
                if (delta > 0 && g_clamped_debt > 0) {
                    long long debt_paid = std::min(g_clamped_debt, delta);
                    effective_delta -= debt_paid;
                    g_clamped_debt  -= debt_paid;
                }
                if (effective_delta != 0) {
                    long long new_rcx = (long long)ctx->Rcx + effective_delta;
                    if (new_rcx < 0) {
                        g_clamped_debt += (long long)(-new_rcx);
                        new_rcx = 0;
                    }
                    ctx->Rcx = (DWORD64)new_rcx;
                    g_last_written_money = new_rcx;
                    if (effective_delta > 0)
                        log("Grant injected at capture: +$" + std::to_string(effective_delta));
                    else
                        log("Fine injected at capture: -$" + std::to_string(-effective_delta) +
                            (g_clamped_debt > 0 ? " (debt: $" + std::to_string(g_clamped_debt) + ")" : ""));
                } else {
                    log("Debt offset at capture: $" + std::to_string(delta) +
                        " grant consumed by debt (remaining: $" + std::to_string(g_clamped_debt) + ")");
                }
                g_applied_money = net_money;
                g_applied_dirty.store(true, std::memory_order_relaxed);
            }
        }
        ctx->Dr0 = 0;
        ctx->Dr7 &= ~(1ULL << 0);
    } else if (ADDR_XP_WRITE && ip == ADDR_XP_WRITE) {
        if (g_xp_ptr.load(std::memory_order_relaxed) == 0) {
            g_xp_ptr.store(ctx->Rsi, std::memory_order_relaxed);
            log("Memory: XP pointer captured " + hex_addr(ctx->Rsi));
            // The instruction (MOV [RSI+0x62C], EDI) hasn't executed yet; EDI holds
            // the new XP value the game is about to write.  Inject our grant delta
            // into EDI (low 32 bits of RDI) so the game commits the combined value.
            int delta = g_items.total_xp_granted - g_applied_xp;
            if (delta > 0) {
                int32_t new_xp = (int32_t)(ctx->Rdi & 0xFFFFFFFF) + delta;
                ctx->Rdi = (ctx->Rdi & 0xFFFFFFFF00000000ULL) | (uint32_t)new_xp;
                g_applied_xp      = g_items.total_xp_granted;
                g_last_written_xp = new_xp;
                g_applied_dirty.store(true, std::memory_order_relaxed);
                log("Grant injected at capture: +" + std::to_string(delta) + " XP");
            }
        }
        ctx->Dr1 = 0;
        ctx->Dr7 &= ~(1ULL << 2);
    } else if (ADDR_CITY_COUNT && ip == ADDR_CITY_COUNT) {
        if (g_city_ptr.load(std::memory_order_relaxed) == 0) {
            g_city_ptr.store(ctx->Rbx, std::memory_order_relaxed);
            log("Memory: city-count pointer captured " + hex_addr(ctx->Rbx));
        }
        ctx->Dr2 = 0;
        ctx->Dr7 &= ~(1ULL << 4);
    } else {
        return EXCEPTION_CONTINUE_SEARCH;
    }

    return EXCEPTION_CONTINUE_EXECUTION;
}

// ── Forward declarations ───────────────────────────────────────────────────────
static void read_items_file();
static void save_applied_state();
static void apply_memory_grants(bool allow_paused = false);
static void flush_events_file();
static void queue_event(const std::string& type, const std::string& game_id,
                        const std::string& key_suffix, json extra = json::object());

// ── Apply pending grants directly to live memory ───────────────────────────────
static void apply_memory_grants(bool allow_paused) {
    std::lock_guard<std::mutex> apply_lock(g_apply_mutex);
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        if (!g_state.in_game && !allow_paused) return;
    }

    // Signal to re-entrant callers (telemetry_paused / telemetry_started) that
    // a write is in progress.  They check this flag and skip their own call so
    // the game's save-triggered pause/resume cycle cannot stack writes.
    g_applying_grant.store(true, std::memory_order_release);

    // ── New-seed detection (under g_apply_mutex — correct lock for applied counters)
    // Two triggers, either of which resets the applied baseline:
    //
    //   1. Seed changed: AP client wrote a different seed_name into items.json,
    //      meaning the player connected to a new multiworld.
    //   2. Negative delta: total_granted dropped below applied_money, which only
    //      happens when a new seed has fewer grants so far than the previous one.
    //      This fallback fires even when seed_name is empty or unavailable.
    {
        bool seed_changed = !g_items.seed.empty() && g_items.seed != g_current_seed;
        bool money_regressed = g_items.total_money_granted > 0 &&
                               g_items.total_money_granted < g_applied_money;
        bool xp_regressed    = g_items.total_xp_granted    > 0 &&
                               g_items.total_xp_granted    < g_applied_xp;

        if (seed_changed || money_regressed || xp_regressed) {
            std::string reason = seed_changed
                ? ("seed changed: " + g_current_seed + " -> " + g_items.seed)
                : "grant totals regressed (new seed without seed field)";
            log("New seed detected (" + reason + ") — resetting applied grant counters"
                " (was: $" + std::to_string(g_applied_money) +
                ", " + std::to_string(g_applied_xp) + " XP)");
            if (seed_changed) g_current_seed = g_items.seed;
            g_applied_money      = 0;
            g_applied_xp         = 0;
            g_clamped_debt       = 0;
            g_last_written_money = LLONG_MIN;
            g_last_written_xp    = INT32_MIN;
            save_applied_state();
        }
    }

    bool needs_rearm = false;

    // Money grant
    uintptr_t mp = g_money_ptr.load(std::memory_order_relaxed);
    if (mp) {
        long long current = safe_read_i64(mp + MONEY_OFFSET);
        // Sanity check: realistic money range 0–10 billion.
        // Values outside this range mean the pointer is stale (object moved after save load).
        if (current < 0 || current > 10000000000LL) {
            log("Money pointer stale (read " + std::to_string(current) +
                ") — resetting and re-arming", SCS_LOG_TYPE_warning);
            g_money_ptr.store(0, std::memory_order_relaxed);
            if (g_money_via_scan.load(std::memory_order_relaxed)) reset_money_scan();
            needs_rearm = true;
        } else {
            // net_money = granted - deducted (traps); delta may be negative (fine).
            long long net_money = g_items.total_money_granted - g_items.total_money_deducted;
            long long delta = net_money - g_applied_money;
            if (delta != 0) {
                // Grants: pay off outstanding clamp-debt first so delivery income is
                // not permanently lost to a fine that exceeded the previous balance.
                long long effective_delta = delta;
                long long new_debt = g_clamped_debt;
                if (delta > 0 && new_debt > 0) {
                    long long debt_paid = std::min(new_debt, delta);
                    effective_delta -= debt_paid;
                    new_debt        -= debt_paid;
                }
                bool applied = false;
                if (effective_delta != 0) {
                    long long newval = current + effective_delta;
                    if (newval < 0) {
                        new_debt += (long long)(-newval);
                        newval = 0;
                    }
                    // Skip if writing would produce the same value we last wrote — this means our
                    // previous write is still in effect and the game hasn't changed the value.
                    // Without this guard a rapid pause/resume cycle can re-apply the same delta.
                    if (newval == g_last_written_money) {
                        log("Grant skip (money): in-memory value matches last write ($" +
                            std::to_string(newval) + ") — marking as applied");
                        applied = true;
                    } else if (safe_write_i64(mp + MONEY_OFFSET, newval)) {
                        g_last_written_money = newval;
                        // g_applied_dirty deferred to frame flush (≤2 s) — do NOT call
                        // save_applied_state() here; each per-write file-write can
                        // trigger an ATS directory-change scan and a game save.
                        g_applied_dirty.store(true, std::memory_order_relaxed);
                        applied = true;
                        if (effective_delta > 0)
                            log("Grant applied (money): +$" + std::to_string(effective_delta) +
                                " → balance $" + std::to_string(newval));
                        else
                            log("Fine applied (money): -$" + std::to_string(-effective_delta) +
                                " → balance $" + std::to_string(newval) +
                                (new_debt > 0 ? " (debt: $" + std::to_string(new_debt) + ")" : ""));
                    } else {
                        log("Grant failed: money write error — resetting pointer", SCS_LOG_TYPE_warning);
                        g_money_ptr.store(0, std::memory_order_relaxed);
                        if (g_money_via_scan.load(std::memory_order_relaxed)) reset_money_scan();
                        needs_rearm = true;
                    }
                } else {
                    // Grant fully consumed by debt — no balance change, debt reduced.
                    log("Debt offset (money): $" + std::to_string(delta) +
                        " grant consumed by debt (remaining: $" + std::to_string(new_debt) + ")");
                    applied = true;
                }
                if (applied) {
                    g_clamped_debt  = new_debt;
                    g_applied_money = net_money;
                    g_applied_dirty.store(true, std::memory_order_relaxed);
                }
            }
        }
    }

    // XP grant
    uintptr_t xp = g_xp_ptr.load(std::memory_order_relaxed);
    if (xp) {
        int32_t current = safe_read_i32(xp + XP_OFFSET);
        // Sanity check: XP must be non-negative and below 2 million.
        // AP grants can push XP well above the normal in-game max, so the bound
        // is intentionally generous — it only needs to catch true garbage reads.
        if (current < 0 || current > 2000000) {
            log("XP pointer stale (read " + std::to_string(current) +
                ") — resetting and re-arming", SCS_LOG_TYPE_warning);
            g_xp_ptr.store(0, std::memory_order_relaxed);
            if (g_xp_via_scan.load(std::memory_order_relaxed)) reset_xp_scan();
            needs_rearm = true;
        } else {
            if (g_items.total_xp_granted > g_applied_xp) {
                int     delta  = g_items.total_xp_granted - g_applied_xp;
                int32_t newval = current + (int32_t)delta;
                if (newval == g_last_written_xp) {
                    log("Grant skip (XP): in-memory value matches last write (" +
                        std::to_string(newval) + " XP) — marking as applied");
                    g_applied_xp = g_items.total_xp_granted;
                } else if (safe_write_i32(xp + XP_OFFSET, newval)) {
                    g_applied_xp      = g_items.total_xp_granted;
                    g_last_written_xp = newval;
                    g_applied_dirty.store(true, std::memory_order_relaxed);
                    // Deferred to frame flush — see money comment above.
                    log("Grant applied (XP): +" + std::to_string(delta) +
                        " XP → total " + std::to_string(newval));
                } else {
                    log("Grant failed: XP write error — resetting pointer", SCS_LOG_TYPE_warning);
                    g_xp_ptr.store(0, std::memory_order_relaxed);
                    if (g_xp_via_scan.load(std::memory_order_relaxed)) reset_xp_scan();
                    needs_rearm = true;
                }
            }
        }
    }

    if (needs_rearm) {
        log("Re-arming breakpoints to re-capture stale pointer(s)...");
        set_bp_all_threads();
    }

    g_applying_grant.store(false, std::memory_order_release);
}

// ── Poll city count for changes ────────────────────────────────────────────────
static void poll_city_count() {
    uintptr_t cp = g_city_ptr.load(std::memory_order_relaxed);
    if (!cp) return;

    uint64_t count = (uint64_t)safe_read_i64(cp + CITY_CNT_OFFSET);

    // Sanity check: ATS has ~700 cities total; any value > 10000 is a garbage read.
    if (count > 10000) {
        log("City pointer stale (read " + std::to_string(count) +
            ") — resetting and re-arming", SCS_LOG_TYPE_warning);
        g_city_ptr.store(0, std::memory_order_relaxed);
        g_prev_city_count = UINT64_MAX;  // reset baseline for next capture
        set_bp_all_threads();
        return;
    }

    if (g_prev_city_count == UINT64_MAX) {
        // First read — baseline, no event.
        g_prev_city_count = count;
        log("City count baseline: " + std::to_string(count));
        return;
    }

    if (count > g_prev_city_count) {
        log("City count changed: " + std::to_string(g_prev_city_count) +
            " -> " + std::to_string(count) + " — signalling client");
        g_prev_city_count = count;

        // Try to name the discovered city directly from memory: probe the
        // registry's backing array for the newest entry and decode it as an
        // SCS token.  The client validates candidates against its city list
        // and falls back to save-file polling when nothing matches, so this
        // is purely an accelerator — it turns "check fires at next autosave"
        // into "check fires the moment the City Discovered banner appears".
        std::vector<std::string> candidates = probe_city_candidates(cp, count);
        float tx, ty, tz;
        {
            std::lock_guard<std::mutex> lock(g_state_mutex);
            g_state.city_count_changed = true;
            tx = g_state.truck_x; ty = g_state.truck_y; tz = g_state.truck_z;
        }
        if (!candidates.empty()) {
            std::string joined;
            for (const auto& c : candidates)
                joined += (joined.empty() ? "" : ", ") + c;
            log("City discovery candidates from memory: [" + joined + "]");
            json extra;
            extra["candidates"] = candidates;
            extra["position"]   = {tx, ty, tz};
            queue_event("city_discovered", "", "citycount_" + std::to_string(count), extra);
        } else {
            log("City discovery: no plausible token found in registry probe — "
                "client will resolve via save file");
        }
        flush_events_file();
    }
}

// ── Read items.json (client → plugin) ─────────────────────────────────────────
static void read_items_file() {
    if (!fs::exists(g_items_file)) return;

    std::lock_guard<std::mutex> lock(g_state_mutex);
    try {
        std::ifstream f(g_items_file);
        json j = json::parse(f);

        g_items.total_money_granted  = (long long)j.value("total_money_granted",  0);
        g_items.total_money_deducted = (long long)j.value("total_money_deducted", 0);
        g_items.total_xp_granted     = j.value("total_xp_granted", 0);
        g_items.win_condition       = j.value("win_condition", 0);
        g_items.goal_level          = j.value("goal_level", 35);
        g_items.goal_money          = (long long)(j.value("goal_money_thousands", 1000)) * 1000;
        g_items.seed                = j.value("seed", std::string(""));
        g_items.save_money          = (long long)j.value("save_money", (long long)-1);
        g_items.save_xp             = j.value("save_xp", -1);
        g_items.last_read_time      = now_seconds();
    } catch (const std::exception& e) {
        log(std::string("Failed to read items.json: ") + e.what(), SCS_LOG_TYPE_warning);
    }
}

// ── Persist / restore applied-grant totals ─────────────────────────────────────
static void load_applied_state() {
    if (!fs::exists(g_applied_file)) return;
    try {
        std::ifstream f(g_applied_file);
        json j = json::parse(f);
        g_applied_money = j.value("applied_money", (long long)0);
        g_applied_xp    = j.value("applied_xp",    0);
        g_clamped_debt  = j.value("clamped_debt",  (long long)0);
        g_current_seed  = j.value("seed",           std::string(""));
        log("Applied state restored: $" + std::to_string(g_applied_money) +
            ", " + std::to_string(g_applied_xp) + " XP" +
            (g_clamped_debt > 0 ? ", debt $" + std::to_string(g_clamped_debt) : "") +
            (g_current_seed.empty() ? "" : " (seed=" + g_current_seed + ")"));
    } catch (...) {}
}

static void save_applied_state() {
    try {
        json j;
        j["applied_money"] = g_applied_money;
        j["applied_xp"]    = g_applied_xp;
        j["clamped_debt"]  = g_clamped_debt;
        j["seed"]          = g_current_seed;
        fs::path tmp = g_applied_file;
        tmp += ".tmp";
        std::ofstream f(tmp);
        f << j.dump(2);
        f.close();
        fs::rename(tmp, g_applied_file);
    } catch (...) {}
}

// ── Write events.json (plugin → client) ───────────────────────────────────────
static void flush_events_file() {
    std::lock_guard<std::mutex> lock(g_state_mutex);

    // Update live values from memory pointers when available.
    uintptr_t mp = g_money_ptr.load(std::memory_order_relaxed);
    uintptr_t xp = g_xp_ptr.load(std::memory_order_relaxed);

    if (mp) g_state.current_money = safe_read_i64(mp + MONEY_OFFSET);
    int32_t live_xp = 0;
    if (xp) {
        live_xp = safe_read_i32(xp + XP_OFFSET);
        g_state.current_level = xp_to_level(live_xp);
    }

    json j;
    j["plugin_alive"]   = g_state.plugin_alive;
    j["plugin_version"] = PLUGIN_VERSION;
    j["game_id"]        = g_game_id;
    j["game_version"]   = g_game_version_str;
    j["timestamp"]      = now_seconds();
    j["engine_wear"]    = g_engine_wear.load(std::memory_order_relaxed);
    j["current_level"]  = g_state.current_level;
    j["current_money"]  = g_state.current_money;
    j["current_xp"]     = live_xp;
    j["truck_position"] = {g_state.truck_x, g_state.truck_y, g_state.truck_z};
    j["in_game"]        = g_state.in_game;
    j["job_active"]     = g_state.job_active;

    // Memory grant status
    j["ptr_money_ready"]    = (mp != 0);
    j["ptr_xp_ready"]       = (xp != 0);
    j["ptr_city_ready"]     = (g_city_ptr.load(std::memory_order_relaxed) != 0);
    // How each pointer was obtained: "scan" if the auto value-scanner found it,
    // "breakpoint" if the SDK-address path captured it, "" if not yet ready.
    j["money_ptr_source"]   = (mp == 0) ? "" :
        (g_money_via_scan.load(std::memory_order_relaxed) ? "scan" : "breakpoint");
    j["xp_ptr_source"]      = (xp == 0) ? "" :
        (g_xp_via_scan.load(std::memory_order_relaxed) ? "scan" : "breakpoint");
    j["applied_money_total"] = g_applied_money;
    j["applied_xp_total"]   = g_applied_xp;

    // Active profile ID (from config.cfg) — client uses this to lock save discovery
    // to the correct profile and never fall back to reading a different profile.
    {
        static std::string s_flushed_pid;
        if (g_active_profile_id != s_flushed_pid) {
            s_flushed_pid = g_active_profile_id;
            log("events.json: active_profile_id=" +
                (g_active_profile_id.empty() ? std::string("(empty)") : g_active_profile_id));
        }
    }
    j["active_profile_id"] = g_active_profile_id;

    // City count change signal (reset after writing so client gets exactly one pulse)
    j["city_count_changed"] = g_state.city_count_changed;
    g_state.city_count_changed = false;

    json events = json::array();
    for (const auto& ev : g_event_queue) {
        json entry;
        entry["id"]      = ev.id;
        entry["type"]    = ev.type;
        entry["game_id"] = ev.game_id;
        for (auto& [k, v] : ev.extra.items()) entry[k] = v;
        events.push_back(entry);
    }
    j["events"] = events;

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
                        const std::string& key_suffix, json extra) {
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
        std::string delivery_id, dest_city;
        {
            std::lock_guard<std::mutex> lock(g_state_mutex);
            if (!g_state.current_cargo_id.empty() && g_state.cargo_loaded) {
                json extra;
                extra["cargo_name"] = g_state.current_cargo_name;
                delivery_id = g_state.current_cargo_id;
                dest_city   = g_state.current_dest_city_id;
                // Emit the destination city hint BEFORE cargo_delivered so the client
                // can process it while job_active is still true (held until delivery flush).
                // Skipped if on_nav_distance already fired it during approach.
                if (!dest_city.empty() && !g_state.dest_hint_sent) {
                    json extra_dest;
                    extra_dest["hint_type"] = "destination";
                    queue_event("city_arrival_hint", dest_city, dest_city, extra_dest);
                    log("Live city arrival hint: " + dest_city + " (destination at delivery — nav hint not sent)");
                }
                g_state.dest_hint_sent = true;
                queue_event("cargo_delivered", delivery_id, delivery_id, extra);
            } else if (!g_state.current_cargo_id.empty()) {
                // job.delivered fired while cargo is not yet loaded — this is the SCS
                // "drive to pickup" phase completion for owned-truck jobs.  Suppress the
                // AP check; it will fire correctly when job.delivered fires after the
                // actual delivery (by which point a new config with cargo_loaded=true
                // will have been received from the game).
                log("job.delivered with cargo not loaded — pickup-entrance phase, skipping AP check"
                    " (cargo=" + g_state.current_cargo_id + ")");
            }
        }
        flush_events_file();

        // Apply any grant that was pending when the delivery completed.
        // This is intentionally a single synchronous call — no timer thread, no loop.
        //
        // Three cases are handled correctly without a timer:
        //   1. Pointer already captured (2nd+ delivery this session): direct write happens here.
        //   2. Pointer captured during this delivery's economy writes (VEH fired just before
        //      job_delivered): VEH already injected the grant at capture; delta is now 0.
        //   3. Pointer not yet captured (delivery ran on an un-breakpointed thread): write is
        //      skipped here (pointer==0); the VEH will inject on the game's next XP/money write,
        //      and telemetry_started + telemetry_frame_start catch any AP items that arrive later.
        //
        // allow_paused=true because the delivery screen pauses the simulation.
        if (!delivery_id.empty() && delivery_id != g_applied_delivery_id) {
            g_applied_delivery_id = delivery_id;
            log("Delivery complete: cargo=" + delivery_id
                + (dest_city.empty() ? "" : " dest_city=" + dest_city)
                + " — applying pending grant once");
            apply_memory_grants(/*allow_paused=*/true);
        } else if (!delivery_id.empty()) {
            log("Duplicate delivery event for cargo=" + delivery_id + " — skipped",
                SCS_LOG_TYPE_warning);
        }
    }

    if (event_name == SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled) {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.job_active = false;
    }
}

SCSAPI_VOID on_truck_placement(const scs_string_t name, const scs_u32_t index,
                                const scs_value_t* const value,
                                const scs_context_t context) {
    if (!value || value->type != SCS_VALUE_TYPE_dplacement) return;
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.truck_x = static_cast<float>(value->value_dplacement.position.x);
    g_state.truck_y = static_cast<float>(value->value_dplacement.position.y);
    g_state.truck_z = static_cast<float>(value->value_dplacement.position.z);
}

// Fires the destination city arrival hint when the navigation distance to the
// delivery drops below DEST_HINT_DISTANCE metres.  This lets the client show
// the city/state grant as the player enters the city rather than only after
// the delivery completes.  Falls back to the delivery-time hint if the player
// has navigation disabled.
SCSAPI_VOID on_nav_distance(const scs_string_t name, const scs_u32_t index,
                             const scs_value_t* const value,
                             const scs_context_t context) {
    if (!value || value->type != SCS_VALUE_TYPE_float) return;
    const float dist = value->value_float.value;

    static constexpr float DEST_HINT_DISTANCE = 1500.0f;
    // Ignore dist==0 (nav not yet computed at job start) and dist near 0
    // (essentially at the parking spot — delivery-time fallback handles that).
    static constexpr float DEST_HINT_MIN_DISTANCE = 50.0f;
    if (dist < DEST_HINT_MIN_DISTANCE || dist > DEST_HINT_DISTANCE) return;

    std::string dest_city;
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        if (!g_state.job_active || g_state.current_dest_city_id.empty() || g_state.dest_hint_sent)
            return;
        dest_city = g_state.current_dest_city_id;
        g_state.dest_hint_sent = true;
    }
    // Telemetry callbacks run on the game thread; queue_event is safe without the mutex here.
    json extra;
    extra["hint_type"] = "destination";
    queue_event("city_arrival_hint", dest_city, dest_city, extra);
    log("Live city arrival hint: " + dest_city
        + " (destination at nav_distance=" + std::to_string(static_cast<int>(dist)) + "m)");
    flush_events_file();
}

// Engine wear channel — drives DeathLink.  When wear crosses the threshold
// while driving, one truck_disabled event is queued for the client (which
// forwards it to the multiworld as a DeathLink if the option is enabled).
SCSAPI_VOID on_engine_wear(const scs_string_t name, const scs_u32_t index,
                            const scs_value_t* const value,
                            const scs_context_t context) {
    if (!value || value->type != SCS_VALUE_TYPE_float) return;
    const float wear = value->value_float.value;
    g_engine_wear.store(wear, std::memory_order_relaxed);

    // First sample shortly after telemetry (re)start only establishes the
    // baseline: a save loaded with an already-destroyed engine must not fire
    // a death.  Samples arriving later than the load window behave normally,
    // so gradual wear crossing the threshold after an unpause still fires.
    if (!g_wear_baselined) {
        g_wear_baselined = true;
        if (wear >= DEATH_WEAR_THRESHOLD &&
            now_seconds() - g_telemetry_started_at < 5.0) {
            g_death_latched = true;
            log("Engine wear baseline " + std::to_string((int)(wear * 100)) +
                "% — already destroyed at load, DeathLink latched without firing");
            return;
        }
    }

    bool in_game;
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        in_game = g_state.in_game;
    }
    if (!in_game) return;

    if (!g_death_latched && wear >= DEATH_WEAR_THRESHOLD) {
        g_death_latched = true;
        ++g_death_counter;
        json extra;
        extra["engine_wear"] = wear;
        // Wall-clock stamp lets the client ignore stale deaths it re-reads
        // from events.json after a client restart.
        extra["epoch"] = (double)std::time(nullptr);
        queue_event("truck_disabled", "engine",
                    "death_" + std::to_string(g_death_counter), extra);
        log("Truck disabled: engine wear " + std::to_string((int)(wear * 100)) +
            "% — queuing DeathLink event");
        flush_events_file();
    } else if (g_death_latched && wear <= DEATH_REARM_THRESHOLD) {
        g_death_latched = false;
        log("Engine repaired to " + std::to_string((int)(wear * 100)) +
            "% — DeathLink re-armed");
    }
}

SCSAPI_VOID telemetry_configuration(const scs_event_t event,
                                     const void* const event_info,
                                     const scs_context_t context) {
    if (!event_info) return;
    const scs_telemetry_configuration_t* const cfg =
        static_cast<const scs_telemetry_configuration_t*>(event_info);
    if (!cfg->id) return;
    if (std::string(cfg->id) != SCS_TELEMETRY_CONFIG_job) return;

    std::string new_cargo_id, new_source_city;
    bool job_just_started = false;
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.current_cargo_id.clear();
        g_state.current_cargo_name.clear();
        g_state.current_source_city_id.clear();
        g_state.current_dest_city_id.clear();
        g_state.job_active      = false;
        g_state.cargo_loaded    = false;
        g_state.dest_hint_sent  = false;

        for (const scs_named_value_t* attr = cfg->attributes; attr->name != nullptr; ++attr) {
            const std::string attr_name(attr->name);
            if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo_id) {
                if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value) {
                    g_state.current_cargo_id = attr->value.value_string.value;
                    g_state.job_active       = true;
                }
            } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo) {
                if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value)
                    g_state.current_cargo_name = attr->value.value_string.value;
            } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_source_city_id) {
                if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value)
                    g_state.current_source_city_id = attr->value.value_string.value;
            } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_destination_city_id) {
                if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value)
                    g_state.current_dest_city_id = attr->value.value_string.value;
            } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_is_cargo_loaded) {
                if (attr->value.type == SCS_VALUE_TYPE_bool)
                    g_state.cargo_loaded = attr->value.value_bool.value;
            }
        }

        if (g_state.job_active) {
            job_just_started = true;
            new_cargo_id    = g_state.current_cargo_id;
            new_source_city = g_state.current_source_city_id;
        }
    }

    if (job_just_started) {
        log("Job started: cargo=" + new_cargo_id
            + (new_source_city.empty() ? "" : " source_city=" + new_source_city));
        // Emit a live city-arrival hint for the source city the moment the job starts.
        // The player is already there; this fires immediately rather than waiting for
        // the game's ks_visit_cities stat update (which can be minutes later).
        if (!new_source_city.empty()) {
            {
                json extra_src;
                extra_src["hint_type"] = "source";
                queue_event("city_arrival_hint", new_source_city, new_source_city, extra_src);
            }
            log("Live city arrival hint: " + new_source_city + " (source city at job start)");
            flush_events_file();
        }
    }
}

// Fires on the Windows thread pool while the simulation is paused (delivery screen).
// frame_start stops during a pause, so this timer bridges the gap between when the
// AP client writes the delivery grant to items.json and when telemetry_started fires
// (the player selects the next job).  g_apply_mutex serialises against any concurrent
// call from telemetry_started.
static VOID CALLBACK pause_grant_timer_cb(PVOID, BOOLEAN) {
    if (g_applying_grant.load(std::memory_order_acquire)) return;
    read_items_file();
    apply_memory_grants(/*allow_paused=*/true);
}

SCSAPI_VOID telemetry_paused(const scs_event_t event, const void* const event_info,
                              const scs_context_t context) {
    // Re-read config.cfg so a mid-session profile switch (main menu → new profile)
    // updates active_profile_id before the next events.json flush.
    // Flush immediately: frame events stop firing while paused, so without this
    // the updated profile ID would never reach the client until the game resumes.
    refresh_active_profile_id();
    flush_events_file();

    // Attempt grant application while in_game is still true (set false below).
    // The re-entrancy guard ensures the job_delivered apply_memory_grants call (which
    // runs synchronously on this same thread just before the pause event) isn't
    // duplicated if the game immediately fires another paused event.
    if (!g_applying_grant.load(std::memory_order_acquire)) {
        read_items_file();
        apply_memory_grants();
    }

    // Start a periodic timer that retries items.json + grant application while
    // the delivery screen is shown.  2 s initial delay gives the AP client time
    // to process the delivery event and write items.json; 1 s period thereafter.
    // Cancelled in telemetry_started.
    if (!g_apply_timer) {
        CreateTimerQueueTimer(&g_apply_timer, NULL,
                              pause_grant_timer_cb, nullptr,
                              2000, 1000,
                              WT_EXECUTEDEFAULT);
    }

    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.in_game = false;
}

SCSAPI_VOID telemetry_started(const scs_event_t event, const void* const event_info,
                               const scs_context_t context) {
    // Cancel the paused-grant timer.  g_apply_mutex inside apply_memory_grants
    // serialises against any callback that may already be in flight.
    if (g_apply_timer) {
        DeleteTimerQueueTimer(NULL, g_apply_timer, NULL);
        g_apply_timer = nullptr;
    }
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.in_game = true;
    }
    // Re-baseline engine wear: a different save/profile may have just been
    // loaded, and its persisted damage must not fire a spurious DeathLink.
    g_wear_baselined = false;
    g_telemetry_started_at = now_seconds();
    // Re-read config.cfg here: the player may have just selected a new profile
    // from the profile screen, and config.cfg is now updated with the new ID.
    // Flush immediately so the client receives the ID before the first save poll.
    refresh_active_profile_id();
    flush_events_file();

    // Apply any grants that arrived while the game was paused (delivery screen, etc.)
    // and that couldn't be written without the pointer being ready.
    if (!g_applying_grant.load(std::memory_order_acquire)) {
        read_items_file();
        apply_memory_grants();
    }
}

// ── Frame timing ───────────────────────────────────────────────────────────────
static double g_last_poll_time  = 0.0;
static double g_last_flush_time = 0.0;
static double g_startup_time   = 0.0;
static const double POLL_INTERVAL_SECONDS  = 0.5;
static const double FLUSH_INTERVAL_SECONDS = 2.0;

SCSAPI_VOID telemetry_frame_start(const scs_event_t event,
                                   const void* const event_info,
                                   const scs_context_t context) {
    double t = now_seconds();

    if (t - g_last_poll_time >= POLL_INTERVAL_SECONDS) {
        g_last_poll_time = t;
        read_items_file();
        apply_memory_grants();
        poll_city_count();
        // Self-find money/XP addresses when the hard-coded ones broke (game
        // update).  No-op once found or when the breakpoint path is working.
        maybe_start_value_scan();
    }

    if (t - g_last_flush_time >= FLUSH_INTERVAL_SECONDS) {
        g_last_flush_time = t;
        flush_events_file();
        if (g_applied_dirty.exchange(false, std::memory_order_relaxed))
            save_applied_state();
    }
}

// ── SCS SDK entry points ───────────────────────────────────────────────────────

SCSAPI_RESULT scs_telemetry_init(const scs_u32_t version,
                                  const scs_telemetry_init_params_t* const params) {
    if (version < SCS_TELEMETRY_VERSION_1_00) return SCS_RESULT_unsupported;

    const scs_telemetry_init_params_v100_t* const p =
        static_cast<const scs_telemetry_init_params_v100_t*>(params);

    g_log = p->common.log;
    g_self = GetCurrentProcess();
    g_game_id = p->common.game_id ? p->common.game_id : "";
    g_game_version_str =
        std::to_string(SCS_GET_MAJOR_VERSION(p->common.game_version)) + "." +
        std::to_string(SCS_GET_MINOR_VERSION(p->common.game_version));
    log("Archipelago plugin v" + std::string(PLUGIN_VERSION) + " initializing"
        " (game=" + g_game_id + " v" + g_game_version_str + ")");

    g_comm_dir     = get_documents_path() / "American Truck Simulator" / "archipelago";
    g_events_file  = g_comm_dir / "events.json";
    g_items_file   = g_comm_dir / "items.json";
    g_applied_file = g_comm_dir / "applied.json";
    if (!fs::exists(g_comm_dir)) fs::create_directories(g_comm_dir);
    log("Comm folder: " + g_comm_dir.string());

    // Read active profile ID from config.cfg so the client can lock save discovery.
    refresh_active_profile_id();

    // Resolve instruction addresses from module base + RVA (with AOB fallback for XP).
    resolve_addresses();

    // Register VEH before setting breakpoints.
    g_veh_handle = AddVectoredExceptionHandler(1, ats_veh);
    if (!g_veh_handle)
        log("WARNING: VEH registration failed — memory grants will not work",
            SCS_LOG_TYPE_warning);
    else
        log("VEH registered; setting hardware breakpoints on all threads");

    set_bp_all_threads();

    load_applied_state();
    read_items_file();

    p->register_for_event(SCS_TELEMETRY_EVENT_frame_start,   telemetry_frame_start,    nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_paused,        telemetry_paused,         nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_started,       telemetry_started,        nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_configuration, telemetry_configuration,  nullptr);
    p->register_for_event(SCS_TELEMETRY_EVENT_gameplay,      telemetry_gameplay_event, nullptr);

    p->register_for_channel(
        SCS_TELEMETRY_TRUCK_CHANNEL_world_placement,
        SCS_U32_NIL, SCS_VALUE_TYPE_dplacement,
        SCS_TELEMETRY_CHANNEL_FLAG_none,
        on_truck_placement, nullptr
    );
    p->register_for_channel(
        SCS_TELEMETRY_TRUCK_CHANNEL_navigation_distance,
        SCS_U32_NIL, SCS_VALUE_TYPE_float,
        SCS_TELEMETRY_CHANNEL_FLAG_none,
        on_nav_distance, nullptr
    );
    p->register_for_channel(
        SCS_TELEMETRY_TRUCK_CHANNEL_wear_engine,
        SCS_U32_NIL, SCS_VALUE_TYPE_float,
        SCS_TELEMETRY_CHANNEL_FLAG_none,
        on_engine_wear, nullptr
    );

    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.plugin_alive = true;
        g_state.in_game      = false;
    }
    flush_events_file();

    g_startup_time = now_seconds();
    log("Plugin ready. Waiting for first money/XP/city event to capture pointers...");
    return SCS_RESULT_ok;
}

SCSAPI_VOID scs_telemetry_shutdown() {
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.plugin_alive = false;
    }
    if (g_apply_timer) {
        DeleteTimerQueueTimer(NULL, g_apply_timer, NULL);
        g_apply_timer = nullptr;
    }
    if (g_applied_dirty.exchange(false, std::memory_order_relaxed))
        save_applied_state();
    flush_events_file();

    if (g_veh_handle) {
        RemoveVectoredExceptionHandler(g_veh_handle);
        g_veh_handle = nullptr;
    }
    clear_bp_all_threads();

    log("Plugin shutdown.");
}

// ── DLL entry point ────────────────────────────────────────────────────────────
BOOL APIENTRY DllMain(HMODULE hModule, DWORD ul_reason_for_call, LPVOID lpReserved) {
    if (ul_reason_for_call == DLL_THREAD_ATTACH && g_veh_handle && !all_ptrs_captured()) {
        // New thread created after init — give it the breakpoints too.
        set_bp_on_thread(GetCurrentThread());
    }
    return TRUE;
}
