#include "mdns_svc.h"

#include "esp_log.h"
#include "mdns.h"

namespace bbfw {

namespace {
const char* const TAG = "bb_mdns";
constexpr uint16_t kPort = 80;
}  // namespace

esp_err_t mdns_svc_start(const std::string& hostname) {
  esp_err_t e = mdns_init();
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "mdns_init: %s", esp_err_to_name(e));
    return e;
  }
  e = mdns_hostname_set(hostname.c_str());
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "mdns_hostname_set: %s", esp_err_to_name(e));
    return e;
  }
  mdns_instance_name_set("BracketBoard scoreboard");

  mdns_txt_item_t txt[] = {
      {const_cast<char*>("v"), const_cast<char*>("1")},
      {const_cast<char*>("path"), const_cast<char*>("/ws")},
  };
  e = mdns_service_add("BracketBoard", "_bracketboard", "_tcp", kPort, txt,
                       sizeof(txt) / sizeof(txt[0]));
  if (e != ESP_OK) ESP_LOGE(TAG, "add _bracketboard._tcp: %s", esp_err_to_name(e));
  esp_err_t e2 = mdns_service_add("BracketBoard", "_http", "_tcp", kPort, nullptr, 0);
  if (e2 != ESP_OK) ESP_LOGE(TAG, "add _http._tcp: %s", esp_err_to_name(e2));

  ESP_LOGI(TAG, "advertising %s.local", hostname.c_str());
  return e != ESP_OK ? e : e2;
}

}  // namespace bbfw
