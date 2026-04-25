# CMake toolchain file for cross-compiling Windows x64 DLL on Linux
# Usage: cmake -B build -DCMAKE_TOOLCHAIN_FILE=mingw-w64-x86_64.cmake

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

# Use the win32 thread model — std::mutex uses native Windows CRITICAL_SECTION,
# no libwinpthread-1.dll dependency, which is required for ATS plugin loading.
find_program(MINGW_CXX NAMES
    x86_64-w64-mingw32-g++-win32
    x86_64-w64-mingw32-g++
    REQUIRED)
find_program(MINGW_CC NAMES
    x86_64-w64-mingw32-gcc-win32
    x86_64-w64-mingw32-gcc
    REQUIRED)
find_program(MINGW_RC NAMES
    x86_64-w64-mingw32-windres
    REQUIRED)

set(CMAKE_C_COMPILER   ${MINGW_CC})
set(CMAKE_CXX_COMPILER ${MINGW_CXX})
set(CMAKE_RC_COMPILER  ${MINGW_RC})

# Tell CMake where to find target-platform libraries and headers
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
