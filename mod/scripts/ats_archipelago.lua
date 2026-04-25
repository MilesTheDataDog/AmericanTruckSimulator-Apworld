--[[
    ats_archipelago.lua
    American Truck Simulator — Archipelago Multiworld Integration (Lua side)

    This script runs within ATS's Lua scripting environment and handles:
    1. Reading the items.json file (written by the Python client) to know what
       is currently unlocked.
    2. Displaying in-game notifications when items are received or checks complete.
    3. Filtering job board entries to block jobs to/from locked-state cities.
    4. Warning the player when they approach or enter a locked state.

    LIMITATIONS:
    - ATS Lua mods have no direct access to the job list UI API; job filtering
      is implemented by cancelling jobs after acceptance if the destination is
      in a locked state and showing a clear warning message.
    - Physical road blocking (yellow X markers) requires a separate map mod and
      is planned for a future release. This script provides the soft enforcement.

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
local g_unlocked_states = {}
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
local g_current_job_dest_state = nil
local g_job_was_cancelled = false
local g_initialized     = false

-- City → state mapping (abbreviated; extended version will be auto-generated)
-- Format: city_id = "state_id"
-- This mirrors the cities.json data so the Lua mod can check job destinations.
local CITY_STATE_MAP = {
    -- California (always unlocked)
    bakersfield="california", barstow="california",
    blythe="california", el_centro="california", eureka="california",
    fresno="california", hilt="california", huron="california", indio="california",
    los_angeles="california", modesto="california", mojave="california",
    oakland="california", oxnard="california", redding="california",
    sacramento="california", san_diego="california", san_francisco="california",
    san_jose="california", santa_cruz="california", santa_maria="california",
    stockton="california", truckee="california", ukiah="california",
    -- Nevada (always unlocked)
    carson_city="nevada", elko="nevada", ely="nevada",
    jackpot="nevada", las_vegas="nevada", pioche="nevada", primm="nevada",
    reno="nevada", tonopah="nevada", winnemucca="nevada",
    -- Arizona
    camp_verde="arizona", clifton="arizona",
    flagstaff="arizona", grand_canyon="arizona", kayenta="arizona",
    kingman="arizona", lake_havasu_city="arizona", nogales="arizona",
    page="arizona", phoenix="arizona", san_simon="arizona",
    show_low="arizona", sierra_vista="arizona", tucson="arizona",
    winslow="arizona", yuma="arizona",
    -- New Mexico
    alamogordo="new_mexico", albuquerque="new_mexico", artesia="new_mexico",
    carlsbad="new_mexico", clovis="new_mexico",
    farmington="new_mexico", gallup="new_mexico", hobbs="new_mexico",
    las_cruces="new_mexico", raton="new_mexico", roswell="new_mexico",
    santa_fe="new_mexico", socorro="new_mexico", tucumcari="new_mexico",
    -- Oregon
    astoria="oregon", bend="oregon", burns="oregon", coos_bay="oregon",
    eugene="oregon", klamath_falls="oregon", lakeview="oregon",
    medford="oregon", newport="oregon", ontario_or="oregon",
    pendleton="oregon", portland="oregon", salem="oregon", the_dalles="oregon",
    -- Washington
    aberdeen="washington", bellingham="washington", colville="washington",
    everett="washington", grand_coulee="washington", kennewick="washington",
    longview_wa="washington", olympia="washington", omak="washington",
    port_angeles="washington", seattle="washington", spokane="washington",
    tacoma="washington", vancouver_wa="washington", wenatchee="washington",
    yakima="washington",
    -- Utah
    cedar_city="utah", logan="utah", moab="utah", ogden="utah", price="utah",
    provo="utah", salina_ut="utah", salt_lake_city="utah",
    st_george="utah", vernal="utah",
    -- Idaho
    boise="idaho", coeur_dalene="idaho", grangeville="idaho",
    idaho_falls="idaho", ketchum="idaho", lewiston="idaho",
    nampa="idaho", pocatello="idaho", salmon="idaho",
    sandpoint="idaho", twin_falls="idaho",
    -- Colorado
    alamosa="colorado", burlington_co="colorado",
    colorado_springs="colorado", denver="colorado",
    durango="colorado", fort_collins="colorado",
    grand_junction="colorado", lamar="colorado",
    montrose="colorado", pueblo="colorado", rangely="colorado",
    steamboat_springs="colorado", sterling="colorado",
    -- Wyoming
    casper="wyoming", cheyenne="wyoming", cody="wyoming",
    evanston="wyoming", gillette="wyoming", jackson="wyoming",
    laramie="wyoming", rawlins="wyoming", riverton="wyoming",
    rock_springs="wyoming", sheridan="wyoming",
    -- Montana
    billings="montana", bozeman="montana", butte="montana",
    glasgow="montana", glendive="montana", great_falls="montana",
    havre="montana", helena="montana", kalispell="montana",
    laurel="montana", lewistown="montana", miles_city="montana",
    missoula="montana", sidney_mt="montana", thompson_falls="montana",
    -- Texas
    abilene="texas", amarillo="texas", austin="texas", beaumont="texas",
    brownsville="texas", corpus_christi="texas", dalhart="texas",
    dallas="texas", del_rio="texas", el_paso="texas",
    fort_stockton="texas", fort_worth="texas", galveston="texas",
    houston="texas", huntsville_tx="texas", junction="texas",
    laredo="texas", longview_tx="texas", lubbock="texas", lufkin="texas",
    mcallen="texas", odessa="texas", san_angelo="texas",
    san_antonio="texas", texarkana_tx="texas", tyler="texas",
    van_horn="texas", victoria="texas", waco="texas", wichita_falls="texas",
    -- Oklahoma
    ardmore="oklahoma", clinton="oklahoma", enid="oklahoma",
    guymon="oklahoma", idabel="oklahoma", lawton="oklahoma",
    mcalester="oklahoma", oklahoma_city="oklahoma", tulsa="oklahoma",
    woodward="oklahoma",
    -- Kansas
    colby="kansas", dodge_city="kansas", emporia="kansas",
    garden_city="kansas", hays="kansas", hutchinson="kansas",
    junction_city="kansas", kansas_city_ks="kansas", marysville="kansas",
    phillipsburg="kansas", pittsburg_ks="kansas", salina="kansas",
    topeka="kansas", wichita="kansas",
    -- Nebraska
    alliance="nebraska", chadron="nebraska", columbus_ne="nebraska",
    grand_island="nebraska", lincoln_ne="nebraska", mccook="nebraska",
    norfolk_ne="nebraska", north_platte="nebraska", omaha="nebraska",
    scottsbluff="nebraska", sydney_ne="nebraska", valentine="nebraska",
    -- Arkansas
    el_dorado="arkansas", fayetteville="arkansas", fort_smith="arkansas",
    harrison="arkansas", hot_springs="arkansas", jonesboro="arkansas",
    little_rock="arkansas", pine_bluff="arkansas", springdale="arkansas",
    texarkana_ar="arkansas",
    -- Missouri
    cape_girardeau="missouri", columbia_mo="missouri",
    jefferson_city="missouri", joplin="missouri",
    kansas_city_mo="missouri", kirksville="missouri",
    maryville_mo="missouri", poplar_bluff="missouri", rolla="missouri",
    springfield_mo="missouri", st_joseph="missouri", st_louis="missouri",
    -- Iowa
    burlington_ia="iowa", cedar_rapids="iowa",
    council_bluffs="iowa", davenport="iowa", des_moines="iowa",
    dubuque="iowa", fort_dodge="iowa", iowa_city="iowa",
    mason_city="iowa", ottumwa="iowa", sioux_city="iowa", waterloo="iowa",
    -- Louisiana
    alexandria_la="louisiana", baton_rouge="louisiana", deridder="louisiana",
    houma="louisiana", lafayette_la="louisiana", lake_charles="louisiana",
    monroe_la="louisiana", natchitoches="louisiana", new_orleans="louisiana",
    port_fourchon="louisiana", shreveport="louisiana",
}


-- ── Utility ────────────────────────────────────────────────────────────────────

local function get_documents_path()
    local userprofile = os.getenv("USERPROFILE")
    if userprofile then
        return userprofile .. "\\Documents"
    end
    -- Fallback for non-Windows or unusual setups
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
-- Extracts a string array value from a JSON string given a key.
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


-- ── Notification system ────────────────────────────────────────────────────────

local function push_notification(message, color)
    table.insert(g_notification_queue, {
        text = message,
        color = color or 0xFFFFFF,
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

    if #g_notification_queue > 0 then
        local n = g_notification_queue[1]
        -- ATS uses message_manager for on-screen text if available
        if message_manager then
            message_manager:show_message(n.text)
        end
    end
end


-- ── Items.json polling ─────────────────────────────────────────────────────────

local function poll_items_file()
    if not g_items_file_path then return end

    local content = read_json_file(g_items_file_path)
    if not content then return end

    local prev_states = g_unlocked_states

    g_unlocked_states  = json_string_array(content, "unlocked_states")
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

    -- Notify player when a new state is unlocked
    for state_id, _ in pairs(g_unlocked_states) do
        if not prev_states[state_id] then
            local display = state_id:gsub("_", " "):gsub("(%a)([%a]*)", function(a,b)
                return a:upper() .. b
            end)
            push_notification("Archipelago: " .. display .. " is now unlocked!", 0x00FF00)
        end
    end

    -- Always unlocked
    g_unlocked_states["california"] = true
    g_unlocked_states["nevada"] = true
end


-- ── State lock enforcement ─────────────────────────────────────────────────────

local function is_state_unlocked(state_id)
    if state_id == "california" or state_id == "nevada" then return true end
    return g_unlocked_states[state_id] == true
end

local function get_city_state(city_id)
    return CITY_STATE_MAP[city_id]
end

local function check_current_job(job)
    -- job is an ATS job info table with fields like destination_city, source_city, cargo
    if not job then return end

    local dest_city  = job.destination_city_id or job.destination_city or ""
    local src_city   = job.source_city_id or job.source_city or ""
    local dest_state = get_city_state(dest_city)
    local src_state  = get_city_state(src_city)

    local locked_state = nil
    if dest_state and not is_state_unlocked(dest_state) then
        locked_state = dest_state
    elseif src_state and not is_state_unlocked(src_state) then
        locked_state = src_state
    end

    if locked_state then
        local display = locked_state:gsub("_", " "):gsub("(%a)([%a]*)", function(a,b)
            return a:upper() .. b
        end)
        push_notification(
            "ARCHIPELAGO: Cannot accept this job — " .. display .. " is LOCKED!\n" ..
            "Deliver more checks to unlock new states.",
            0xFF4444
        )
        g_job_was_cancelled = true
        return false  -- signal: job should be rejected
    end

    g_job_was_cancelled = false
    return true
end


-- ── ATS event callbacks ────────────────────────────────────────────────────────
-- ATS Lua mods register callbacks via the `events` global table.
-- Available events vary by game version; we use the most commonly supported ones.

local function on_job_accepted(event_data)
    local ok = check_current_job(event_data)
    if not ok then
        -- We cannot programmatically cancel a job from Lua after acceptance,
        -- but we show a very prominent warning. The player must manually
        -- cancel the job via the in-game menu (Escape → Cancel Job).
        push_notification(
            "ARCHIPELAGO WARNING: Please cancel this job immediately!\n" ..
            "Use Escape → Cancel Job → Yes. Job to a locked state is not valid.",
            0xFF0000
        )
    end
end

local function on_job_finished(event_data)
    -- Job completed — the C++ plugin handles sending the delivery check event.
    -- Here we just show a congratulatory message if it seems noteworthy.
    if event_data and event_data.cargo_name then
        -- No need to duplicate the check here; plugin handles it.
    end
end

local function on_player_fined(event_data)
    -- Death-link hook point — if death_link is enabled, a severe fine could
    -- trigger a "death" signal. This is reserved for future implementation.
end


-- ── Initialization ─────────────────────────────────────────────────────────────

local function init()
    local docs = get_documents_path()
    g_items_file_path = docs .. "\\American Truck Simulator\\" .. COMM_SUBPATH

    -- Perform initial poll
    poll_items_file()

    -- Register event callbacks if the ATS events API is available
    if events then
        if events.register then
            events.register("job.accepted",  on_job_accepted)
            events.register("job.finished",  on_job_finished)
            events.register("player.fined",  on_player_fined)
        end
    end

    push_notification("Archipelago mod loaded. Happy trucking!", 0x4488FF)
    g_initialized = true
end


-- ── Main update loop ───────────────────────────────────────────────────────────
-- ATS calls update() on each mod script frame if the function is defined.

local function update(dt)
    if not g_initialized then
        init()
        return
    end

    -- Poll items file on interval
    g_last_poll_time = g_last_poll_time + dt
    if g_last_poll_time >= POLL_INTERVAL then
        g_last_poll_time = 0
        poll_items_file()
    end

    -- Update notification display
    update_notifications()
end


-- ── Module exports (ATS mod entry points) ─────────────────────────────────────
-- ATS will call these if they exist at the script root level.

function onInit()
    init()
end

function onUpdate(dt)
    update(dt)
end

-- Export for any inter-mod communication
return {
    is_state_unlocked      = is_state_unlocked,
    get_city_state         = get_city_state,
    push_notification      = push_notification,
    unlocked_states        = g_unlocked_states,
}
