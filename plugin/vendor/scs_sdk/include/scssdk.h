/**
 * scssdk.h - SCS Software SDK core types
 * Reconstructed from public SCS SDK documentation and known plugin examples.
 */
#ifndef SCSSDK_H
#define SCSSDK_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Calling convention */
#ifdef _WIN32
#  define SCSAPI_CALL __stdcall
#else
#  define SCSAPI_CALL
#endif

/* Scalar types */
typedef uint8_t     scs_u8_t;
typedef uint16_t    scs_u16_t;
typedef uint32_t    scs_u32_t;
typedef uint64_t    scs_u64_t;
typedef int8_t      scs_s8_t;
typedef int16_t     scs_s16_t;
typedef int32_t     scs_s32_t;
typedef int64_t     scs_s64_t;
typedef float       scs_float_t;
typedef double      scs_double_t;
typedef const char *scs_string_t;
typedef void       *scs_context_t;
typedef scs_u8_t    scs_bool_t;

#define SCS_FALSE       ((scs_bool_t)0)
#define SCS_TRUE        ((scs_bool_t)1)
#define SCS_U32_NIL     ((scs_u32_t)0xFFFFFFFFU)
#define SCS_U64_NIL     ((scs_u64_t)0xFFFFFFFFFFFFFFFFULL)

/* Result codes */
typedef scs_s32_t scs_result_t;
#define SCS_RESULT_ok                   ((scs_result_t) 0)
#define SCS_RESULT_unsupported          ((scs_result_t) 1)
#define SCS_RESULT_invalid_parameter    ((scs_result_t) 2)
#define SCS_RESULT_already_registered   ((scs_result_t) 3)
#define SCS_RESULT_not_found            ((scs_result_t) 4)
#define SCS_RESULT_unsupported_type     ((scs_result_t) 5)
#define SCS_RESULT_not_now              ((scs_result_t) 6)
#define SCS_RESULT_generic_error        ((scs_result_t)-1)

/* Log types */
typedef scs_u32_t scs_log_type_t;
#define SCS_LOG_TYPE_message    ((scs_log_type_t)0)
#define SCS_LOG_TYPE_warning    ((scs_log_type_t)1)
#define SCS_LOG_TYPE_error      ((scs_log_type_t)2)

typedef void (SCSAPI_CALL *scs_log_t)(const scs_log_type_t type, const scs_string_t message);

/* Return-type macros */
#define SCSAPI_RESULT   scs_result_t SCSAPI_CALL
#define SCSAPI_VOID     void SCSAPI_CALL

#ifdef __cplusplus
}
#endif

#endif /* SCSSDK_H */
