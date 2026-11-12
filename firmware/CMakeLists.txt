# BracketBoard ESP32 firmware (ESP-IDF 5.x). See README.md.
cmake_minimum_required(VERSION 3.16)

# bb_core wraps the portable ../core sources as an IDF component.
set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/components)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(bracketboard)
