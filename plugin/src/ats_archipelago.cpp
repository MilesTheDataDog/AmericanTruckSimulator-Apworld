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
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>
#include <set>
#include <map>
#include <mutex>
#include <atomic>
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
static const char* PLUGIN_VERSION = "2.2.0";

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
    bool      job_active = false;
    bool      in_game    = false;
    bool      city_count_changed = false;
};

static GameState g_state;

// ── Item state (read from items.json) ─────────────────────────────────────────
struct ItemState {
    long long total_money_granted = 0;
    int       total_xp_granted    = 0;
    int       win_condition       = 0;
    int       goal_level          = 35;
    long long goal_money          = 1000000;
    double    last_read_time      = 0.0;
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
static long long g_applied_money = 0;
static int       g_applied_xp    = 0;
static std::atomic<bool> g_applied_dirty{false};

// Previous city count — detects increases without keeping the breakpoint live.
static uint64_t g_prev_city_count = UINT64_MAX; // UINT64_MAX = uninitialized

static std::mutex              g_apply_mutex;          // prevents timer + game thread double-applying
static std::atomic<bool>       g_delivery_grant_pending{false};

static PVOID g_veh_handle = nullptr;

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

    // ── Money ──────────────────────────────────────────────────────────────────
    {
        uintptr_t addr = base + MONEY_RVA;
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
                "  (money grants disabled — update FRIEND_BASE or provide new address)",
                SCS_LOG_TYPE_warning);
        }
    }

    // ── XP — try RVA first, then AOB scan (6-byte pattern is fairly unique) ──
    {
        uintptr_t addr = base + XP_RVA;
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
        uintptr_t addr = base + CITY_RVA;
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
                "  (city detection disabled — update FRIEND_BASE or provide new address)",
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
            // the new balance the game is about to write.  Inject our grant delta
            // into RCX now so the game's own instruction commits the combined value.
            long long delta = (long long)g_items.total_money_granted - (long long)g_applied_money;
            if (delta > 0) {
                ctx->Rcx = (DWORD64)((long long)ctx->Rcx + delta);
                g_applied_money = g_items.total_money_granted;
                g_applied_dirty.store(true, std::memory_order_relaxed);
                log("Grant injected at capture: +$" + std::to_string(delta));
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
                g_applied_xp = g_items.total_xp_granted;
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

// ── Forward declarations (timer calls these before their definitions) ──────────
static void read_items_file();
static void save_applied_state();
static void apply_memory_grants(bool allow_paused = false);

// ── Delivery-screen grant timer ────────────────────────────────────────────────
// Spawned by job_delivered; polls items.json during the delivery summary screen
// so grants reach the player before they dismiss the screen, not at the next delivery.
static DWORD WINAPI grant_timer_func(LPVOID) {
    // Snapshot applied totals at timer start.  We exit early only when the
    // applied totals INCREASE (i.e. a grant was actually written to memory),
    // not when they merely equal total_granted (that just means items.json
    // hasn't been updated by the AP server yet — the whole point of this timer).
    long long snap_money = g_applied_money;
    int       snap_xp    = g_applied_xp;

    const int MAX_POLLS = 20;  // 20 × 500 ms = 10 s maximum wait
    for (int i = 0; i < MAX_POLLS; ++i) {
        Sleep(500);
        if (!g_delivery_grant_pending.load(std::memory_order_relaxed)) break;
        read_items_file();
        apply_memory_grants(true);  // allow_paused = true
        // Exit once a grant was successfully applied.
        if (g_applied_money > snap_money || g_applied_xp > snap_xp)
            break;
    }
    g_delivery_grant_pending.store(false, std::memory_order_relaxed);
    return 0;
}

static void start_grant_timer() {
    if (g_delivery_grant_pending.load(std::memory_order_relaxed)) return;
    g_delivery_grant_pending.store(true, std::memory_order_relaxed);
    HANDLE th = CreateThread(nullptr, 0, grant_timer_func, nullptr, 0, nullptr);
    if (th) CloseHandle(th);  // fire-and-forget; thread cleans itself up
}

// ── Apply pending grants directly to live memory ───────────────────────────────
static void apply_memory_grants(bool allow_paused) {
    std::lock_guard<std::mutex> apply_lock(g_apply_mutex);
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        if (!g_state.in_game && !allow_paused) return;
    }

    bool needs_rearm = false;

    // Money grant
    uintptr_t mp = g_money_ptr.load(std::memory_order_relaxed);
    if (mp && g_items.total_money_granted > g_applied_money) {
        long long current = safe_read_i64(mp + MONEY_OFFSET);
        // Sanity check: realistic money range 0–10 billion.
        // Values outside this range mean the pointer is stale (object moved after save load).
        if (current < 0 || current > 10000000000LL) {
            log("Money pointer stale (read " + std::to_string(current) +
                ") — resetting and re-arming", SCS_LOG_TYPE_warning);
            g_money_ptr.store(0, std::memory_order_relaxed);
            needs_rearm = true;
        } else {
            long long delta  = g_items.total_money_granted - g_applied_money;
            long long newval = current + delta;
            if (safe_write_i64(mp + MONEY_OFFSET, newval)) {
                g_applied_money = g_items.total_money_granted;
                g_applied_dirty.store(true, std::memory_order_relaxed);
                log("Grant applied: +$" + std::to_string(delta) +
                    " (balance now $" + std::to_string(newval) + ")");
                save_applied_state();
            } else {
                log("Grant failed: money write error — resetting pointer", SCS_LOG_TYPE_warning);
                g_money_ptr.store(0, std::memory_order_relaxed);
                needs_rearm = true;
            }
        }
    }

    // XP grant
    uintptr_t xp = g_xp_ptr.load(std::memory_order_relaxed);
    if (xp && g_items.total_xp_granted > g_applied_xp) {
        int32_t current = safe_read_i32(xp + XP_OFFSET);
        // Sanity check: XP is 0–200000 (level 1 to max).
        if (current < 0 || current > 200000) {
            log("XP pointer stale (read " + std::to_string(current) +
                ") — resetting and re-arming", SCS_LOG_TYPE_warning);
            g_xp_ptr.store(0, std::memory_order_relaxed);
            needs_rearm = true;
        } else {
            int     delta  = g_items.total_xp_granted - g_applied_xp;
            int32_t newval = current + (int32_t)delta;
            if (safe_write_i32(xp + XP_OFFSET, newval)) {
                g_applied_xp = g_items.total_xp_granted;
                g_applied_dirty.store(true, std::memory_order_relaxed);
                log("Grant applied: +" + std::to_string(delta) +
                    " XP (total now " + std::to_string(newval) + ")");
                save_applied_state();
            } else {
                log("Grant failed: XP write error — resetting pointer", SCS_LOG_TYPE_warning);
                g_xp_ptr.store(0, std::memory_order_relaxed);
                needs_rearm = true;
            }
        }
    }

    if (needs_rearm) {
        log("Re-arming breakpoints to re-capture stale pointer(s)...");
        set_bp_all_threads();
    }
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
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.city_count_changed = true;
    }
}

// ── Read items.json (client → plugin) ─────────────────────────────────────────
static void read_items_file() {
    if (!fs::exists(g_items_file)) return;

    std::lock_guard<std::mutex> lock(g_state_mutex);
    try {
        std::ifstream f(g_items_file);
        json j = json::parse(f);

        g_items.total_money_granted = (long long)j.value("total_money_granted", 0);
        g_items.total_xp_granted    = j.value("total_xp_granted", 0);
        g_items.win_condition       = j.value("win_condition", 0);
        g_items.goal_level          = j.value("goal_level", 35);
        g_items.goal_money          = (long long)(j.value("goal_money_thousands", 1000)) * 1000;
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
        log("Applied state restored: $" + std::to_string(g_applied_money) +
            ", " + std::to_string(g_applied_xp) + " XP");
    } catch (...) {}
}

static void save_applied_state() {
    try {
        json j;
        j["applied_money"] = g_applied_money;
        j["applied_xp"]    = g_applied_xp;
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
    j["timestamp"]      = now_seconds();
    j["current_level"]  = g_state.current_level;
    j["current_money"]  = g_state.current_money;
    j["current_xp"]     = live_xp;
    j["truck_position"] = {g_state.truck_x, g_state.truck_y, g_state.truck_z};
    j["in_game"]        = g_state.in_game;

    // Memory grant status
    j["ptr_money_ready"]    = (mp != 0);
    j["ptr_xp_ready"]       = (xp != 0);
    j["ptr_city_ready"]     = (g_city_ptr.load(std::memory_order_relaxed) != 0);
    j["applied_money_total"] = g_applied_money;
    j["applied_xp_total"]   = g_applied_xp;

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
        start_grant_timer();
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
                g_state.current_cargo_id = attr->value.value_string.value;
                g_state.job_active       = true;
            }
        } else if (attr_name == SCS_TELEMETRY_CONFIG_ATTRIBUTE_cargo) {
            if (attr->value.type == SCS_VALUE_TYPE_string && attr->value.value_string.value)
                g_state.current_cargo_name = attr->value.value_string.value;
        }
    }

    if (g_state.job_active)
        log("Job started: cargo=" + g_state.current_cargo_id);
}

SCSAPI_VOID telemetry_paused(const scs_event_t event, const void* const event_info,
                              const scs_context_t context) {
    // Read items.json and attempt grant application before marking in_game=false.
    // This covers the case where the player pauses immediately after dismissing the
    // delivery screen, before frame_start has had a chance to pick up items.json.
    read_items_file();
    apply_memory_grants();
    std::lock_guard<std::mutex> lock(g_state_mutex);
    g_state.in_game = false;
}

SCSAPI_VOID telemetry_started(const scs_event_t event, const void* const event_info,
                               const scs_context_t context) {
    g_delivery_grant_pending.store(false, std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lock(g_state_mutex);
        g_state.in_game = true;
    }
    read_items_file();
    apply_memory_grants();
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
    log("Archipelago plugin v" + std::string(PLUGIN_VERSION) + " initializing");

    g_comm_dir     = get_documents_path() / "American Truck Simulator" / "archipelago";
    g_events_file  = g_comm_dir / "events.json";
    g_items_file   = g_comm_dir / "items.json";
    g_applied_file = g_comm_dir / "applied.json";
    if (!fs::exists(g_comm_dir)) fs::create_directories(g_comm_dir);
    log("Comm folder: " + g_comm_dir.string());

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
