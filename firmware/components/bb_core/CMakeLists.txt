# Compiles the portable core (../../../core) as an ESP-IDF component.
# core/ has no ESP dependencies; cJSON is vendored and kept private to this component
# (main/ adds the cJSON include dir privately for /config parsing and links the same symbols).
set(BB_CORE_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../core")

# NOTE: re-run `idf.py reconfigure` after adding a new file under core/src.
file(GLOB BB_CORE_SRCS "${BB_CORE_DIR}/src/*.cpp")

idf_component_register(
    SRCS ${BB_CORE_SRCS} "${BB_CORE_DIR}/third_party/cjson/cJSON.c"
    INCLUDE_DIRS "${BB_CORE_DIR}/include"
    PRIV_INCLUDE_DIRS "${BB_CORE_DIR}/third_party/cjson"
)

# Not scriptable, so skip during IDF's early requirements expansion.
if(NOT CMAKE_BUILD_EARLY_EXPANSION)
    target_compile_options(${COMPONENT_LIB} PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-std=gnu++17>)
    # Vendored third-party code: keep the same -w as the host build.
    set_source_files_properties("${BB_CORE_DIR}/third_party/cjson/cJSON.c"
                                PROPERTIES COMPILE_FLAGS "-w")
endif()
