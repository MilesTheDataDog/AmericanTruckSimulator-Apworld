/**
 * scssdk_value.h - SCS SDK value types
 * Reconstructed from public SCS SDK documentation and known plugin examples.
 */
#ifndef SCSSDK_VALUE_H
#define SCSSDK_VALUE_H

#include "scssdk.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Value type tags */
typedef scs_u32_t scs_value_type_t;
#define SCS_VALUE_TYPE_INVALID      ((scs_value_type_t) 0)
#define SCS_VALUE_TYPE_bool         ((scs_value_type_t) 1)
#define SCS_VALUE_TYPE_s32          ((scs_value_type_t) 2)
#define SCS_VALUE_TYPE_u32          ((scs_value_type_t) 3)
#define SCS_VALUE_TYPE_u64          ((scs_value_type_t) 4)
#define SCS_VALUE_TYPE_float        ((scs_value_type_t) 5)
#define SCS_VALUE_TYPE_double       ((scs_value_type_t) 6)
#define SCS_VALUE_TYPE_fvector      ((scs_value_type_t) 7)
#define SCS_VALUE_TYPE_dvector      ((scs_value_type_t) 8)
#define SCS_VALUE_TYPE_euler        ((scs_value_type_t) 9)
#define SCS_VALUE_TYPE_fplacement   ((scs_value_type_t)10)
#define SCS_VALUE_TYPE_dplacement   ((scs_value_type_t)11)
#define SCS_VALUE_TYPE_string       ((scs_value_type_t)12)
#define SCS_VALUE_TYPE_s64          ((scs_value_type_t)13)

/* Vector / placement structs */
#pragma pack(push, 1)

typedef struct {
    scs_float_t x, y, z;
} scs_fvector_t;

typedef struct {
    scs_double_t x, y, z;
} scs_dvector_t;

typedef struct {
    scs_float_t heading, pitch, roll;
} scs_euler_t;

typedef struct {
    scs_fvector_t   position;
    scs_euler_t     orientation;
} scs_fplacement_t;

typedef struct {
    scs_dvector_t   position;
    scs_euler_t     orientation;
} scs_dplacement_t;

/* Individual typed value structs (each wraps one scalar/compound) */
typedef struct { scs_u8_t          value; } scs_value_bool_t;
typedef struct { scs_s32_t         value; } scs_value_s32_t;
typedef struct { scs_u32_t         value; } scs_value_u32_t;
typedef struct { scs_u64_t         value; } scs_value_u64_t;
typedef struct { scs_s64_t         value; } scs_value_s64_t;
typedef struct { scs_float_t       value; } scs_value_float_t;
typedef struct { scs_double_t      value; } scs_value_double_t;
typedef struct { scs_fvector_t     value; } scs_value_fvector_t;
typedef struct { scs_dvector_t     value; } scs_value_dvector_t;
typedef struct { scs_euler_t       value; } scs_value_euler_t;
typedef struct { scs_fplacement_t  value; } scs_value_fplacement_t;
typedef struct { scs_dplacement_t  value; } scs_value_dplacement_t;
typedef struct { scs_string_t      value; } scs_value_string_t;

/* Tagged union — anonymous inner union so members are accessible directly */
typedef struct {
    scs_value_type_t    type;
    scs_u32_t           _padding;
    union {
        scs_value_bool_t        value_bool;
        scs_value_s32_t         value_s32;
        scs_value_u32_t         value_u32;
        scs_value_u64_t         value_u64;
        scs_value_s64_t         value_s64;
        scs_value_float_t       value_float;
        scs_value_double_t      value_double;
        scs_value_fvector_t     value_fvector;
        scs_value_dvector_t     value_dvector;
        scs_value_euler_t       value_euler;
        scs_value_fplacement_t  value_fplacement;
        scs_value_dplacement_t  value_dplacement;
        scs_value_string_t      value_string;
    };
} scs_value_t;

/* Named value (used in config/attribute tables) */
typedef struct {
    scs_string_t    name;
    scs_u32_t       index;
    scs_value_t     value;
} scs_named_value_t;

#pragma pack(pop)

#ifdef __cplusplus
}
#endif

#endif /* SCSSDK_VALUE_H */
