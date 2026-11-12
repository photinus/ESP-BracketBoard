#include "config_store.h"

#include <cstring>

#include "esp_log.h"
#include "esp_random.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

namespace bbfw {

namespace {

const char* const TAG = "bb_cfg";
const char* const kNs = "bb";
const char* const kDefaultHost = "scoreboard";
const char* const kDefaultApPass = "bracketboard";

bool all_of_chars(const std::string& s, bool (*ok)(char)) {
  for (char c : s) {
    if (!ok(c)) return false;
  }
  return true;
}
bool is_token_char(char c) {
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
         c == '_' || c == '-';
}
bool is_host_char(char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || c == '-'; }

bool ok_ssid(const std::string& s) { return s.size() <= 32; }
bool ok_pass(const std::string& s) { return s.empty() || (s.size() >= 8 && s.size() <= 63); }
bool ok_ap_pass(const std::string& s) { return s.size() >= 8 && s.size() <= 63; }
bool ok_token(const std::string& s) {
  return s.size() >= 8 && s.size() <= 32 && all_of_chars(s, is_token_char);
}
bool ok_host(const std::string& s) {
  return !s.empty() && s.size() <= 32 && s.front() != '-' && s.back() != '-' &&
         all_of_chars(s, is_host_char);
}
bool ok_stale(int32_t v) { return v >= 2000 && v <= 10000; }

bool nvs_get_string(nvs_handle_t h, const char* key, std::string& out) {
  size_t len = 0;
  if (nvs_get_str(h, key, nullptr, &len) != ESP_OK || len == 0) return false;
  std::string buf(len, '\0');
  if (nvs_get_str(h, key, &buf[0], &len) != ESP_OK) return false;
  buf.resize(strnlen(buf.c_str(), len));  // drop the trailing NUL
  out = std::move(buf);
  return true;
}

void defaults(Config& c) {
  c.ssid = CONFIG_BB_WIFI_SSID;
  c.pass = CONFIG_BB_WIFI_PASS;
  c.token = CONFIG_BB_TOKEN;
  c.host = CONFIG_BB_HOSTNAME;
  c.ap_pass = CONFIG_BB_AP_PASS;
  c.stale_ms = CONFIG_BB_STALE_TIMEOUT_MS;
#ifdef CONFIG_BB_MANUAL_ENABLED
  c.manual_en = true;
#else
  c.manual_en = false;
#endif
}

void sanitise(Config& c) {
  if (!ok_ssid(c.ssid)) {
    ESP_LOGW(TAG, "invalid ssid, clearing");
    c.ssid.clear();
  }
  if (!ok_pass(c.pass)) {
    ESP_LOGW(TAG, "invalid wifi password length, clearing");
    c.pass.clear();
  }
  if (!c.token.empty() && !ok_token(c.token)) {
    // Do not echo the token; treat as unset so a fresh one is generated.
    ESP_LOGW(TAG, "stored token is invalid (need 8-32 chars of [A-Za-z0-9_-]); ignoring");
    c.token.clear();
  }
  if (!ok_host(c.host)) {
    ESP_LOGW(TAG, "invalid hostname, using '%s'", kDefaultHost);
    c.host = kDefaultHost;
  }
  if (!ok_ap_pass(c.ap_pass)) {
    ESP_LOGW(TAG, "invalid AP password (need 8-63 chars), using built-in default");
    c.ap_pass = kDefaultApPass;
  }
  if (!ok_stale(c.stale_ms)) {
    ESP_LOGW(TAG, "stale_ms out of range, using 3000");
    c.stale_ms = 3000;
  }
}

std::string generate_token() {
  static const char kAlphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  std::string t;
  while (t.size() < 8) {
    uint32_t r = esp_random() & 0xFF;
    if (r >= 248) continue;  // 248 = 62 * 4: reject to avoid modulo bias
    t.push_back(kAlphabet[r % 62]);
  }
  return t;
}

}  // namespace

const char* config_validate(const ConfigUpdate& u) {
  if (u.ssid && !ok_ssid(*u.ssid)) return "ssid";
  if (u.pass && !ok_pass(*u.pass)) return "pass";
  if (u.token && !ok_token(*u.token)) return "token";
  if (u.host && !ok_host(*u.host)) return "host";
  if (u.ap_pass && !ok_ap_pass(*u.ap_pass)) return "ap_pass";
  if (u.stale_ms && !ok_stale(*u.stale_ms)) return "stale_ms";
  return nullptr;
}

esp_err_t config_init() {
  esp_err_t e = nvs_flash_init();
  if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_LOGW(TAG, "NVS needs erase (%s)", esp_err_to_name(e));
    ESP_ERROR_CHECK(nvs_flash_erase());
    e = nvs_flash_init();
  }
  return e;
}

esp_err_t config_read(Config& out) {
  defaults(out);
  nvs_handle_t h;
  esp_err_t e = nvs_open(kNs, NVS_READONLY, &h);
  if (e == ESP_OK) {
    nvs_get_string(h, "ssid", out.ssid);
    nvs_get_string(h, "pass", out.pass);
    nvs_get_string(h, "token", out.token);
    nvs_get_string(h, "host", out.host);
    nvs_get_string(h, "ap_pass", out.ap_pass);
    int32_t sm;
    if (nvs_get_i32(h, "stale_ms", &sm) == ESP_OK) out.stale_ms = sm;
    uint8_t me;
    if (nvs_get_u8(h, "manual_en", &me) == ESP_OK) out.manual_en = (me != 0);
    nvs_close(h);
  } else if (e != ESP_ERR_NVS_NOT_FOUND) {  // NOT_FOUND = namespace never written: defaults
    ESP_LOGW(TAG, "nvs_open failed: %s (using defaults)", esp_err_to_name(e));
  } else {
    e = ESP_OK;
  }
  sanitise(out);
  return e;
}

esp_err_t config_load(Config& out, bool& token_generated) {
  token_generated = false;
  esp_err_t e = config_read(out);
  if (out.token.empty()) {
    out.token = generate_token();
    token_generated = true;
    ConfigUpdate u;
    u.token = out.token;
    esp_err_t se = config_save(u);
    if (se != ESP_OK) {
      ESP_LOGE(TAG, "could not persist generated token (%s); it changes every boot",
               esp_err_to_name(se));
      if (e == ESP_OK) e = se;
    }
  }
  return e;
}

esp_err_t config_save(const ConfigUpdate& u) {
  nvs_handle_t h;
  esp_err_t e = nvs_open(kNs, NVS_READWRITE, &h);
  if (e != ESP_OK) return e;
  auto put = [&](const char* key, const std::optional<std::string>& v) {
    if (e == ESP_OK && v) e = nvs_set_str(h, key, v->c_str());
  };
  put("ssid", u.ssid);
  put("pass", u.pass);
  put("token", u.token);
  put("host", u.host);
  put("ap_pass", u.ap_pass);
  if (e == ESP_OK && u.stale_ms) e = nvs_set_i32(h, "stale_ms", *u.stale_ms);
  if (e == ESP_OK && u.manual_en) e = nvs_set_u8(h, "manual_en", *u.manual_en ? 1 : 0);
  if (e == ESP_OK) e = nvs_commit(h);
  nvs_close(h);
  return e;
}

}  // namespace bbfw
