/**
 * amtrucks/scssdk_telemetry_ats.h - ATS-specific telemetry channel and event names
 * Reconstructed from public SCS SDK documentation and modding resources.
 */
#ifndef SCSSDK_TELEMETRY_ATS_H
#define SCSSDK_TELEMETRY_ATS_H

#include "../scssdk_telemetry.h"
#include "scssdk_ats.h"

/* ── Gameplay event IDs ───────────────────────────────────────────────────── */
#define SCS_TELEMETRY_GAMEPLAY_EVENT_job_delivered          "job.delivered"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_job_cancelled          "job.cancelled"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_job_finished           "job.finished"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_transport_load         "transport.load"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_transport_unload       "transport.unload"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_player_fined           "player.fined"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_player_tollgate_paid   "player.tollgate.paid"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_ferry       "player.use.ferry"
#define SCS_TELEMETRY_GAMEPLAY_EVENT_player_use_train       "player.use.train"

/* ── Configuration IDs ────────────────────────────────────────────────────── */
#define SCS_TELEMETRY_CONFIG_substances     "substances"
#define SCS_TELEMETRY_CONFIG_controls       "controls"
#define SCS_TELEMETRY_CONFIG_hshifter       "hshifter"
#define SCS_TELEMETRY_CONFIG_truck          "truck"
#define SCS_TELEMETRY_CONFIG_trailer        "trailer"
#define SCS_TELEMETRY_CONFIG_job            "job"

/* ── Truck telemetry channels ─────────────────────────────────────────────── */

/* Position / orientation */
#define SCS_TELEMETRY_TRUCK_CHANNEL_world_placement         "truck.world.placement"
#define SCS_TELEMETRY_TRUCK_CHANNEL_local_linear_velocity   "truck.local.velocity.linear"
#define SCS_TELEMETRY_TRUCK_CHANNEL_local_angular_velocity  "truck.local.velocity.angular"
#define SCS_TELEMETRY_TRUCK_CHANNEL_local_linear_acceleration   "truck.local.acceleration.linear"
#define SCS_TELEMETRY_TRUCK_CHANNEL_local_angular_acceleration  "truck.local.acceleration.angular"

/* Cabin / head placement */
#define SCS_TELEMETRY_TRUCK_CHANNEL_cabin_offset            "truck.cabin.offset"
#define SCS_TELEMETRY_TRUCK_CHANNEL_head_offset             "truck.head.offset"

/* Powertrain */
#define SCS_TELEMETRY_TRUCK_CHANNEL_speed                   "truck.speed"
#define SCS_TELEMETRY_TRUCK_CHANNEL_engine_rpm              "truck.engine.rpm"
#define SCS_TELEMETRY_TRUCK_CHANNEL_engine_rpm_max          "truck.engine.rpm.max"
#define SCS_TELEMETRY_TRUCK_CHANNEL_fuel                    "truck.fuel.amount"
#define SCS_TELEMETRY_TRUCK_CHANNEL_fuel_warning            "truck.fuel.warning"
#define SCS_TELEMETRY_TRUCK_CHANNEL_cruise_control          "truck.cruise_control"
#define SCS_TELEMETRY_TRUCK_CHANNEL_gear_dash               "truck.engine.gear.dash"
#define SCS_TELEMETRY_TRUCK_CHANNEL_displayed_gear          "truck.displayed.gear"
#define SCS_TELEMETRY_TRUCK_CHANNEL_motor_gear_driving      "truck.engine.gear.driving"

/* Brakes / suspension */
#define SCS_TELEMETRY_TRUCK_CHANNEL_parking_brake           "truck.brake.parking"
#define SCS_TELEMETRY_TRUCK_CHANNEL_motor_brake             "truck.brake.motor"

/* Lights / indicators */
#define SCS_TELEMETRY_TRUCK_CHANNEL_lights_lblinker         "truck.light.lblinker"
#define SCS_TELEMETRY_TRUCK_CHANNEL_lights_rblinker         "truck.light.rblinker"
#define SCS_TELEMETRY_TRUCK_CHANNEL_lights_parking          "truck.light.parking"
#define SCS_TELEMETRY_TRUCK_CHANNEL_lights_low_beam         "truck.light.beam.low"
#define SCS_TELEMETRY_TRUCK_CHANNEL_lights_high_beam        "truck.light.beam.high"

/* Damage */
#define SCS_TELEMETRY_TRUCK_CHANNEL_wear_engine             "truck.wear.engine"
#define SCS_TELEMETRY_TRUCK_CHANNEL_wear_transmission       "truck.wear.transmission"
#define SCS_TELEMETRY_TRUCK_CHANNEL_wear_cabin              "truck.wear.cabin"
#define SCS_TELEMETRY_TRUCK_CHANNEL_wear_chassis            "truck.wear.chassis"
#define SCS_TELEMETRY_TRUCK_CHANNEL_wear_wheels             "truck.wear.wheels"

/* Navigation */
#define SCS_TELEMETRY_TRUCK_CHANNEL_navigation_distance     "truck.navigation.distance"
#define SCS_TELEMETRY_TRUCK_CHANNEL_navigation_time         "truck.navigation.time"
#define SCS_TELEMETRY_TRUCK_CHANNEL_navigation_speed_limit  "truck.navigation.speed.limit"

/* ── Trailer channels (indexed) ───────────────────────────────────────────── */
#define SCS_TELEMETRY_TRAILER_CHANNEL_connected             "trailer.connection"
#define SCS_TELEMETRY_TRAILER_CHANNEL_world_placement       "trailer.world.placement"
#define SCS_TELEMETRY_TRAILER_CHANNEL_wear_chassis          "trailer.wear.chassis"

#endif /* SCSSDK_TELEMETRY_ATS_H */
