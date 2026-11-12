# Portable core library (host build). The firmware builds the same sources via
# firmware/components/bb_core.
add_library(bb_core STATIC
  src/types.cpp
  src/snapshot.cpp
  src/seq_filter.cpp
  src/clock_model.cpp
  src/frame.cpp
  src/manual.cpp
  src/auth.cpp
  src/board.cpp
  src/control_hub.cpp
  third_party/cjson/cJSON.c
)
target_include_directories(bb_core PUBLIC include)
target_include_directories(bb_core PRIVATE third_party/cjson)
target_compile_features(bb_core PUBLIC cxx_std_17)
set_source_files_properties(third_party/cjson/cJSON.c PROPERTIES COMPILE_FLAGS "-w")
if(NOT MSVC)
  target_compile_options(bb_core PRIVATE -Wall -Wextra)
endif()
