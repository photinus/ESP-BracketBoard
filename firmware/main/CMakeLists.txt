idf_component_register(
    SRCS "main.cpp" "wifi_mgr.cpp" "mdns_svc.cpp" "net_server.cpp"
         "config_store.cpp" "panel_display.cpp"
    INCLUDE_DIRS "."
    # cJSON.h (vendored in core/) is used only for GET/POST /config. The symbols come from
    # the bb_core archive; do NOT also require IDF's `json` component (duplicate symbols).
    PRIV_INCLUDE_DIRS "../../core/third_party/cjson"
    REQUIRES bb_core
    PRIV_REQUIRES esp_wifi esp_netif esp_event esp_http_server esp_timer nvs_flash
                  esp_system log espressif__mdns
    EMBED_TXTFILES "web/manual.html"
)
