--[[
    ats_archipelago.lua
    American Truck Simulator — Archipelago Multiworld Integration (Lua side)

    This script runs within ATS's Lua scripting environment and handles:
    1. Reading the items.json file (written by the Python client) to know what
       is currently unlocked.
    2. Displaying in-game notifications when items are received or checks complete.

    All DLC states the player owns are freely accessible — no state locking is
    enforced. Unlock items in the multiworld pool cover trucks, garages, and
    recruitment offices only.

    File communication:
    - Reads:  %USERPROFILE%\Documents\American Truck Simulator\archipelago\items.json
    - Writes: Nothing (read-only from Lua side; C++ plugin handles writes)
]]

-- ── Configuration ──────────────────────────────────────────────────────────────
local POLL_INTERVAL     = 3.0   -- seconds between items.json reads
local NOTIFY_DURATION   = 6.0   -- seconds to display each notification
local COMM_SUBPATH      = "archipelago\\items.json"

-- ── State ──────────────────────────────────────────────────────────────────────
local g_items_file_path = nil
local g_last_poll_time  = 0
local g_unlocked_trucks = {}
local g_unlocked_garages = {}
local g_unlocked_offices = {}
local g_shuffle_trucks  = false
local g_shuffle_garages = false
local g_shuffle_offices = false
local g_win_condition   = 0
local g_goal_level      = 35
local g_goal_money      = 1000000
local g_notification_queue = {}
local g_initialized     = false
-- Tracks the highest item_notification id we have already shown the player,
-- so we only show each received-item popup once across polls.
local g_last_shown_notification_id = -1


-- ── Utility ────────────────────────────────────────────────────────────────────

local function get_documents_path()
    local userprofile = os.getenv("USERPROFILE")
    if userprofile then
        return userprofile .. "\\Documents"
    end
    return os.getenv("HOME") or "."
end

local function read_json_file(path)
    local f = io.open(path, "r")
    if not f then return nil end
    local content = f:read("*all")
    f:close()
    return content
end

-- Minimal JSON value extractor (no full parser — avoids external dependencies)
local function json_string_array(json_str, key)
    local result = {}
    local pattern = '"' .. key .. '"%s*:%s*%[([^%]]*)%]'
    local arr_str = json_str:match(pattern)
    if arr_str then
        for val in arr_str:gmatch('"([^"]*)"') do
            result[val] = true
        end
    end
    return result
end

local function json_number(json_str, key)
    local pattern = '"' .. key .. '"%s*:%s*(-?%d+%.?%d*)'
    local val = json_str:match(pattern)
    return val and tonumber(val) or nil
end

local function json_bool(json_str, key)
    local pattern = '"' .. key .. '"%s*:%s*(true|false)'
    local val = json_str:match(pattern)
    return val == "true"
end

-- Parse the item_notifications array written by the Python client.
local function json_notification_items(json_str)
    local result = {}
    local arr = json_str:match('"item_notifications"%s*:%s*(%b[])')
    if not arr then return result end
    for obj in arr:gmatch('%b{}') do
        local id   = tonumber(obj:match('"id"%s*:%s*(%d+)'))
        local text = obj:match('"text"%s*:%s*"([^"]*)"')
        if id ~= nil and text then
            text = text:gsub('\\"', '"'):gsub('\\\\', '\\')
            table.insert(result, { id = id, text = text })
        end
    end
    table.sort(result, function(a, b) return a.id < b.id end)
    return result
end


-- ── Notification system ────────────────────────────────────────────────────────

local function display_message(text)
    if message_manager then
        local ok = pcall(function()
            if message_manager.push_message then
                message_manager:push_message(text, NOTIFY_DURATION)
            elseif message_manager.show_message then
                message_manager:show_message(text)
            end
        end)
        if ok then return end
    end
    print("[ATS-Archipelago] " .. text)
end

local function push_notification(message, color)
    display_message(message)
    table.insert(g_notification_queue, {
        text   = message,
        color  = color or 0xFFFFFF,
        expire = os.clock() + NOTIFY_DURATION,
    })
end

local function update_notifications()
    local now = os.clock()
    local active = {}
    for _, n in ipairs(g_notification_queue) do
        if n.expire > now then
            table.insert(active, n)
        end
    end
    g_notification_queue = active
end


-- ── Items.json polling ─────────────────────────────────────────────────────────

local function poll_items_file()
    if not g_items_file_path then return end

    local content = read_json_file(g_items_file_path)
    if not content then return end

    g_unlocked_trucks  = json_string_array(content, "unlocked_trucks")
    g_unlocked_garages = json_string_array(content, "unlocked_garages")
    g_unlocked_offices = json_string_array(content, "unlocked_offices")
    g_shuffle_trucks   = json_bool(content, "shuffle_trucks")
    g_shuffle_garages  = json_bool(content, "shuffle_garages")
    g_shuffle_offices  = json_bool(content, "shuffle_recruitment_offices")
    g_win_condition    = json_number(content, "win_condition") or 0
    g_goal_level       = json_number(content, "goal_level") or 35
    local goal_k       = json_number(content, "goal_money_thousands") or 1000
    g_goal_money       = goal_k * 1000

    -- Show in-game popups for items received from the Archipelago multiworld.
    local notifications = json_notification_items(content)
    for _, notif in ipairs(notifications) do
        if notif.id > g_last_shown_notification_id then
            push_notification("AP: " .. notif.text, 0x00DDFF)
            g_last_shown_notification_id = notif.id
        end
    end
end


-- ── Initialization ─────────────────────────────────────────────────────────────

local function init()
    local docs = get_documents_path()
    g_items_file_path = docs .. "\\American Truck Simulator\\" .. COMM_SUBPATH

    poll_items_file()

    push_notification("Archipelago mod loaded. Happy trucking!", 0x4488FF)
    g_initialized = true
end


-- ── Main update loop ───────────────────────────────────────────────────────────

local function update(dt)
    if not g_initialized then
        init()
        return
    end

    g_last_poll_time = g_last_poll_time + dt
    if g_last_poll_time >= POLL_INTERVAL then
        g_last_poll_time = 0
        poll_items_file()
    end

    update_notifications()
end


-- ── Module exports (ATS mod entry points) ─────────────────────────────────────

function onInit()
    init()
end

function onUpdate(dt)
    update(dt)
end

return {
    push_notification  = push_notification,
    unlocked_trucks    = g_unlocked_trucks,
    unlocked_garages   = g_unlocked_garages,
    unlocked_offices   = g_unlocked_offices,
}
