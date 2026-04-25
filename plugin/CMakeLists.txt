cmake_minimum_required(VERSION 3.20)
project(ats_archipelago CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── SCS SDK ───────────────────────────────────────────────────────────────────
# Download the SCS SDK from: https://modding.scssoft.com/wiki/SDK
# Extract it so that scssdk_telemetry.h is at:
#     <project_root>/plugin/vendor/scs_sdk/include/scssdk_telemetry.h
set(SCS_SDK_INCLUDE "${CMAKE_SOURCE_DIR}/vendor/scs_sdk/include"
    CACHE PATH "Path to SCS SDK include directory")

if(NOT EXISTS "${SCS_SDK_INCLUDE}/scssdk_telemetry.h")
    message(FATAL_ERROR
        "SCS SDK headers not found at ${SCS_SDK_INCLUDE}.\n"
        "Download the SDK from https://modding.scssoft.com/wiki/SDK and extract it to:\n"
        "    ${CMAKE_SOURCE_DIR}/vendor/scs_sdk/\n"
        "so that scssdk_telemetry.h is at:\n"
        "    ${SCS_SDK_INCLUDE}/scssdk_telemetry.h"
    )
endif()

# ── nlohmann/json ─────────────────────────────────────────────────────────────
# Download the single-header version from: https://github.com/nlohmann/json/releases
# Place json.hpp at: <project_root>/plugin/vendor/nlohmann/json.hpp
# Include path is the *parent* of the nlohmann/ directory so that
# #include "nlohmann/json.hpp" resolves correctly.
set(NLOHMANN_JSON_INCLUDE "${CMAKE_SOURCE_DIR}/vendor"
    CACHE PATH "Parent directory containing nlohmann/json.hpp")

if(NOT EXISTS "${NLOHMANN_JSON_INCLUDE}/nlohmann/json.hpp")
    message(FATAL_ERROR
        "nlohmann/json.hpp not found at ${NLOHMANN_JSON_INCLUDE}/nlohmann/json.hpp.\n"
        "Download json.hpp from https://github.com/nlohmann/json/releases/latest\n"
        "and place it at: ${CMAKE_SOURCE_DIR}/vendor/nlohmann/json.hpp"
    )
endif()

# ── Plugin target ─────────────────────────────────────────────────────────────
add_library(ats_archipelago SHARED
    src/ats_archipelago.cpp
)

target_include_directories(ats_archipelago PRIVATE
    ${SCS_SDK_INCLUDE}
    ${NLOHMANN_JSON_INCLUDE}
)

target_compile_definitions(ats_archipelago PRIVATE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    _CRT_SECURE_NO_WARNINGS
)

# Output DLL name must match what ATS expects in the plugins folder
set_target_properties(ats_archipelago PROPERTIES
    OUTPUT_NAME "ats_archipelago"
    PREFIX ""
    SUFFIX ".dll"
)

# Link required Windows libraries
target_link_libraries(ats_archipelago PRIVATE
    shell32    # SHGetFolderPath
    shlwapi
)

# MinGW cross-compilation: statically link the C++ runtime so the DLL is
# self-contained and doesn't require MinGW runtime DLLs on the target machine.
if(MINGW)
    target_link_options(ats_archipelago PRIVATE
        -static-libgcc
        -static-libstdc++
        -Wl,--kill-at          # stdcall name decoration compatible with MSVC callers
    )
    # std::filesystem on MinGW-w64 GCC 13 is built-in; no extra link needed.
endif()

# ── Install target ────────────────────────────────────────────────────────────
# Run: cmake --install . --prefix "<ATS install path>"
# This copies the DLL to the correct plugins folder.
install(TARGETS ats_archipelago
    RUNTIME DESTINATION "bin/win_x64/plugins"
)

# ── Build instructions in README ──────────────────────────────────────────────
message(STATUS "")
message(STATUS "ATS Archipelago Plugin")
message(STATUS "======================")
message(STATUS "To build:")
message(STATUS "  1. Install CMake 3.20+ and Visual Studio 2022 (or MSVC toolchain)")
message(STATUS "  2. Download SCS SDK → place at ${CMAKE_SOURCE_DIR}/vendor/scs_sdk/")
message(STATUS "  3. Download nlohmann/json.hpp → place at ${CMAKE_SOURCE_DIR}/vendor/nlohmann/json.hpp")
message(STATUS "  4. Configure:  cmake -B build -A x64")
message(STATUS "  5. Build:      cmake --build build --config Release")
message(STATUS "  6. Install:    cmake --install build --prefix \"<ATS install path>\"")
message(STATUS "     OR copy build/Release/ats_archipelago.dll manually to:")
message(STATUS "        <ATS>\\bin\\win_x64\\plugins\\ats_archipelago.dll")
message(STATUS "")
