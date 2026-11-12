# Top-level host build:  cmake -S host -B build && cmake --build build && ctest --test-dir build
cmake_minimum_required(VERSION 3.16)
project(bracketboard_host CXX C)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Debug)
endif()
enable_testing()

add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../core core_build)

add_library(bb_host STATIC console_display.cpp)
target_include_directories(bb_host PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(bb_host PUBLIC bb_core)

# Stdio bridge used by the Python end-to-end tests (see docs/architecture.md).
add_executable(bb_core_cli bb_core_cli.cpp)
target_include_directories(bb_core_cli PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../core/third_party/cjson)
target_link_libraries(bb_core_cli PRIVATE bb_host)

# Unit tests: every tests/test_*.cpp is compiled into one doctest binary.
# -DBB_ONLY_TESTS="test_a.cpp;test_b.cpp" builds just those (lets parallel workers stay independent).
if(BB_ONLY_TESTS)
  set(BB_TEST_SOURCES "")
  foreach(t ${BB_ONLY_TESTS})
    list(APPEND BB_TEST_SOURCES ${CMAKE_CURRENT_SOURCE_DIR}/tests/${t})
  endforeach()
else()
  file(GLOB BB_TEST_SOURCES CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_*.cpp)
endif()
add_executable(bb_tests tests/test_main.cpp ${BB_TEST_SOURCES})
target_include_directories(bb_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/tests
                           ${CMAKE_CURRENT_SOURCE_DIR}/../core/third_party/cjson)
target_link_libraries(bb_tests PRIVATE bb_host)
target_compile_definitions(bb_tests PRIVATE
  BB_FIXTURE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/../docs/fixtures")
add_test(NAME bb_tests COMMAND bb_tests)
