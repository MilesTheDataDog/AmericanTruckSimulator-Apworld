/**
 * scssdk_telemetry.h - SCS SDK Telemetry API
 * Reconstructed from public SCS SDK documentation and known plugin examples.
 *
 * The plugin DLL must export:
 *   SCSAPI_RESULT scs_telemetry_init(scs_u32_t version, const scs_telemetry_init_params_t* params)
 *   SCSAPI_VOID   scs_telemetry_shutdown(void)
 */
#ifndef SCSSDK_TELEMETRY_H
#define SCSSDK_TELEMETRY_H

#include "scssdk.h"
#include "scssdk_value.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ── API version ──────────────────────────────────────────────────────────── */
#define SCS_TELEMETRY_VERSION_1_00  ((scs_u32_t)0x00000100)

/* ── Telemetry events ─────────────────────────────────────────────────────── */
typedef scs_u32_t scs_event_t;
#define SCS_TELEMETRY_EVENT_invalid         ((scs_event_t)0)
#define SCS_TELEMETRY_EVENT_frame_start     ((scs_event_t)1)
#define SCS_TELEMETRY_EVENT_frame_end       ((scs_event_t)2)
#define SCS_TELEMETRY_EVENT_paused          ((scs_event_t)3)
#define SCS_TELEMETRY_EVENT_started         ((scs_event_t)4)
#define SCS_TELEMETRY_EVENT_configuration   ((scs_event_t)5)
#define SCS_TELEMETRY_EVENT_gameplay        ((scs_event_t)6)

/* ── Channel flags ────────────────────────────────────────────────────────── */
typedef scs_u32_t scs_telemetry_channel_flag_t;
#define SCS_TELEMETRY_CHANNEL_FLAG_none         ((scs_telemetry_channel_flag_t)0x00000000)
#define SCS_TELEMETRY_CHANNEL_FLAG_each_frame   ((scs_telemetry_channel_flag_t)0x00000001)
#define SCS_TELEMETRY_CHANNEL_FLAG_no_value     ((scs_telemetry_channel_flag_t)0x00000002)

/* ── Gameplay event info structure ────────────────────────────────────────── */
#pragma pack(push, 1)
typedef struct {
    scs_string_t        id;     /* e.g. "job.delivered", "job.cancelled" */
    scs_u32_t           flags;
    /* Followed by a null-terminated scs_named_value_t attribute array,
     * but we only need the id field for our purposes. */
} scs_telemetry_gameplay_event_t;

/* ── Configuration event info structure ───────────────────────────────────── */
typedef struct {
    scs_string_t            id;
    scs_u32_t               flags;
    const scs_named_value_t *attributes;
} scs_telemetry_configuration_t;

/* ── Frame start event info ───────────────────────────────────────────────── */
typedef struct {
    scs_u64_t   paused_simulation_time;
    scs_u64_t   render_time;
    scs_u64_t   simulation_time;
    scs_u64_t   multi_player_time_offset;
} scs_telemetry_frame_start_t;
#pragma pack(pop)

/* ── Callback typedefs ────────────────────────────────────────────────────── */
typedef void (SCSAPI_CALL *scs_telemetry_event_callback_t)(
    const scs_event_t           event,
    const void *const           event_info,
    const scs_context_t         context);

typedef void (SCSAPI_CALL *scs_telemetry_channel_callback_t)(
    const scs_string_t          name,
    const scs_u32_t             index,
    const scs_value_t *const    value,
    const scs_context_t         context);

/* ── Register / unregister function pointer types ─────────────────────────── */
typedef scs_result_t (SCSAPI_CALL *scs_telemetry_register_for_event_t)(
    const scs_event_t                       event,
    const scs_telemetry_event_callback_t    callback,
    const scs_context_t                     context);

typedef scs_result_t (SCSAPI_CALL *scs_telemetry_unregister_from_event_t)(
    const scs_event_t event);

typedef scs_result_t (SCSAPI_CALL *scs_telemetry_register_channel_t)(
    const scs_string_t                      name,
    const scs_u32_t                         index,
    const scs_value_type_t                  type,
    const scs_u32_t                         flags,
    const scs_telemetry_channel_callback_t  callback,
    const scs_context_t                     context);

typedef scs_result_t (SCSAPI_CALL *scs_telemetry_unregister_from_channel_t)(
    const scs_string_t      name,
    const scs_u32_t         index,
    const scs_value_type_t  type);

/* ── Common SDK init params (v1.00) ───────────────────────────────────────── */
/* Field order must exactly match the real SCS SDK struct:
 *   +0  game_version  u32   (4 bytes)
 *   +4  game_id       ptr   (8 bytes on x64)
 *   +12 game_build    u32   (4 bytes)
 *   +16 log           fptr  (8 bytes on x64)
 * There is NO sdk_version field — that was a mistake in the initial stub. */
#pragma pack(push, 1)
typedef struct {
    scs_u32_t       game_version;
    scs_string_t    game_id;
    scs_u32_t       game_build;
    scs_log_t       log;
} scs_sdk_init_params_v100_t;

/* ── Telemetry init params (v1.00) ────────────────────────────────────────── */
typedef struct {
    scs_sdk_init_params_v100_t              common;
    scs_telemetry_register_for_event_t      register_for_event;
    scs_telemetry_unregister_from_event_t   unregister_from_event;
    scs_telemetry_register_channel_t        register_channel;
    scs_telemetry_unregister_from_channel_t unregister_from_channel;
} scs_telemetry_init_params_v100_t;
#pragma pack(pop)

/* Opaque base type used in the init function signature */
typedef void scs_telemetry_init_params_t;

/* ── DLL entry points the plugin must export ──────────────────────────────── */
SCSAPI_RESULT scs_telemetry_init(
    const scs_u32_t                         version,
    const scs_telemetry_init_params_t *const params);

SCSAPI_VOID scs_telemetry_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* SCSSDK_TELEMETRY_H */
