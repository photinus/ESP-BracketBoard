// Runtime configuration: Kconfig defaults overridden by NVS namespace "bb".
// Changes made through save() apply on the next reboot.
#pragma once
#include <cstdint>
#include <optional>
#include <string>
#include "esp_err.h"

namespace bbfw {

struct Config {
  std::string ssid;
  std::string pass;
  std::string token;
  std::string host;
  std::string ap_pass;
  int32_t stale_ms = 3000;
  bool manual_en = false;
};

// Partial update (POST /config). Absent fields are left untouched.
struct ConfigUpdate {
  std::optional<std::string> ssid, pass, token, host, ap_pass;
  std::optional<int32_t> stale_ms;
  std::optional<bool> manual_en;
};

// Returns the name of the first invalid field, or nullptr if the update is acceptable.
const char* config_validate(const ConfigUpdate& u);

// nvs_flash_init (erasing on version mismatch). Call once, first.
esp_err_t config_init();

// Kconfig defaults + NVS overrides, sanitised. Never generates a token and never
// returns an invalid value. If NVS cannot be opened, `out` holds the defaults.
esp_err_t config_read(Config& out);

// config_read + first-boot token generation (8 alphanumerics, esp_random, persisted).
// `token_generated` is true only on the boot that created the token.
esp_err_t config_load(Config& out, bool& token_generated);

// Validates nothing (call config_validate first); persists and commits.
esp_err_t config_save(const ConfigUpdate& u);

}  // namespace bbfw
