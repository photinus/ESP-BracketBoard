#include "wifi_mgr.h"

#include <cstdio>
#include <cstring>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"

namespace bbfw {

namespace {
const char* const TAG = "bb_wifi";
constexpr int kMaxRetries = 5;
constexpr int64_t kStaConnectTimeoutUs = 15LL * 1000 * 1000;
constexpr int64_t kStaRetryPeriodUs = 30LL * 1000 * 1000;
constexpr int64_t kApDropAfterUs = 60LL * 1000 * 1000;
}  // namespace

esp_err_t WifiMgr::start(const WifiSettings& s) {
  s_ = s;
  have_ssid_ = !s_.ssid.empty();

  esp_err_t e = esp_netif_init();
  if (e != ESP_OK) return e;
  e = esp_event_loop_create_default();
  if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) return e;  // INVALID_STATE: already created

  sta_netif_ = esp_netif_create_default_wifi_sta();
  ap_netif_ = esp_netif_create_default_wifi_ap();
  if (!sta_netif_ || !ap_netif_) return ESP_FAIL;
  esp_netif_set_hostname(sta_netif_, s_.host.c_str());

  wifi_init_config_t icfg = WIFI_INIT_CONFIG_DEFAULT();
  e = esp_wifi_init(&icfg);
  if (e != ESP_OK) return e;
  esp_wifi_set_storage(WIFI_STORAGE_RAM);  // credentials live in our NVS namespace, not the driver's

  q_ = xQueueCreate(8, sizeof(Ev));
  if (!q_) return ESP_ERR_NO_MEM;
  e = esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, &WifiMgr::on_event, this);
  if (e != ESP_OK) return e;
  e = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &WifiMgr::on_event, this);
  if (e != ESP_OK) return e;

  // AP SSID: BracketBoard-XXXX (last two bytes of the soft-AP MAC).
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
  snprintf(ap_ssid_, sizeof(ap_ssid_), "BracketBoard-%02X%02X", mac[4], mac[5]);

  // Pre-load the STA config (kept in RAM across mode switches).
  wifi_config_t sta = {};
  size_t sl = s_.ssid.size() > sizeof(sta.sta.ssid) ? sizeof(sta.sta.ssid) : s_.ssid.size();
  memcpy(sta.sta.ssid, s_.ssid.data(), sl);
  strlcpy(reinterpret_cast<char*>(sta.sta.password), s_.pass.c_str(), sizeof(sta.sta.password));
  sta.sta.threshold.authmode = s_.pass.empty() ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;
  sta.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
  sta.sta.pmf_cfg.capable = true;
  sta.sta.pmf_cfg.required = false;

  const int64_t now = esp_timer_get_time();
  attempt_start_us_ = now;
  if (have_ssid_) {
    e = esp_wifi_set_mode(WIFI_MODE_STA);
    if (e != ESP_OK) return e;
    e = esp_wifi_set_config(WIFI_IF_STA, &sta);
    if (e != ESP_OK) return e;
    phase_ = Phase::StaConnecting;
    e = esp_wifi_start();
    if (e != ESP_OK) return e;
    ESP_LOGI(TAG, "connecting to SSID '%s'", s_.ssid.c_str());
    connect_sta("initial");
  } else {
    ESP_LOGW(TAG, "no SSID configured: starting setup access point");
    // enter_fallback() sets the AP-only mode + AP config; wifi is started afterwards.
    enter_fallback(now);
    e = esp_wifi_start();
    if (e != ESP_OK) return e;
  }

  if (xTaskCreate(&WifiMgr::task_entry, "bb_wifi", 4096, this, 5, nullptr) != pdPASS) {
    return ESP_ERR_NO_MEM;
  }
  return ESP_OK;
}

void WifiMgr::on_event(void* arg, esp_event_base_t base, int32_t id, void* data) {
  auto* self = static_cast<WifiMgr*>(arg);
  Ev ev{};
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    ev.type = EvType::StaDisconnected;
    ev.reason = data ? static_cast<uint8_t>(
                           static_cast<wifi_event_sta_disconnected_t*>(data)->reason)
                     : 0;
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    ev.type = EvType::StaGotIp;
    ev.reason = 0;
    if (data) {
      auto* ip = static_cast<ip_event_got_ip_t*>(data);
      ESP_LOGI(TAG, "got IP " IPSTR, IP2STR(&ip->ip_info.ip));
    }
  } else {
    return;
  }
  xQueueSend(self->q_, &ev, 0);  // drop on overflow; timers recover the state machine
}

void WifiMgr::task_entry(void* arg) { static_cast<WifiMgr*>(arg)->task(); }

void WifiMgr::task() {
  for (;;) {
    Ev ev;
    if (xQueueReceive(q_, &ev, pdMS_TO_TICKS(500)) == pdTRUE) {
      handle_event(ev, esp_timer_get_time());
    }
    poll_timers(esp_timer_get_time());
  }
}

void WifiMgr::connect_sta(const char* why) {
  esp_err_t e = esp_wifi_connect();
  if (e != ESP_OK) ESP_LOGW(TAG, "esp_wifi_connect (%s): %s", why, esp_err_to_name(e));
}

void WifiMgr::handle_event(const Ev& ev, int64_t now) {
  switch (ev.type) {
    case EvType::StaGotIp:
      sta_connected_ = true;
      retries_ = 0;
      connected_since_us_ = now;
      if (phase_ == Phase::StaConnecting) phase_ = Phase::StaUp;
      // ApFallback: stay until the 60 s timer in poll_timers() drops the AP.
      break;

    case EvType::StaDisconnected:
      sta_connected_ = false;
      connected_since_us_ = 0;
      ESP_LOGW(TAG, "STA disconnected (reason %u)", ev.reason);
      if (phase_ == Phase::StaConnecting) {
        if (++retries_ < kMaxRetries) {
          connect_sta("retry");
        } else {
          ESP_LOGW(TAG, "STA failed %d times", retries_);
          enter_fallback(now);
        }
      } else if (phase_ == Phase::StaUp) {
        // Lost an established link: try again (15 s / 5 attempts), then fall back.
        phase_ = Phase::StaConnecting;
        retries_ = 0;
        attempt_start_us_ = now;
        connect_sta("reconnect");
      }
      // ApFallback: the 30 s retry timer drives reconnection.
      break;
  }
}

void WifiMgr::poll_timers(int64_t now) {
  switch (phase_) {
    case Phase::StaConnecting:
      if (!sta_connected_ && now - attempt_start_us_ > kStaConnectTimeoutUs) {
        ESP_LOGW(TAG, "STA connect timeout");
        enter_fallback(now);
      }
      break;

    case Phase::ApFallback:
      if (sta_connected_ && connected_since_us_ != 0 && now - connected_since_us_ > kApDropAfterUs) {
        leave_fallback();
      } else if (have_ssid_ && !sta_connected_ && now - last_retry_us_ > kStaRetryPeriodUs) {
        last_retry_us_ = now;
        connect_sta("fallback retry");
      }
      break;

    case Phase::StaUp:
      break;
  }
}

void WifiMgr::enter_fallback(int64_t now) {
  wifi_config_t ap = {};
  size_t n = strlen(ap_ssid_);
  memcpy(ap.ap.ssid, ap_ssid_, n);
  ap.ap.ssid_len = static_cast<uint8_t>(n);
  strlcpy(reinterpret_cast<char*>(ap.ap.password), s_.ap_pass.c_str(), sizeof(ap.ap.password));
  ap.ap.channel = 1;
  ap.ap.max_connection = 4;
  ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
  ap.ap.pmf_cfg.required = false;

  // The mode must include the AP interface before its config can be set.
  esp_err_t e = esp_wifi_set_mode(have_ssid_ ? WIFI_MODE_APSTA : WIFI_MODE_AP);
  if (e != ESP_OK) ESP_LOGE(TAG, "set mode: %s", esp_err_to_name(e));
  e = esp_wifi_set_config(WIFI_IF_AP, &ap);
  if (e != ESP_OK) ESP_LOGE(TAG, "set AP config: %s", esp_err_to_name(e));

  phase_ = Phase::ApFallback;
  ap_active_ = true;
  last_retry_us_ = now;
  ESP_LOGW(TAG, "access point '%s' up at 192.168.4.1", ap_ssid_);
}

void WifiMgr::leave_fallback() {
  esp_err_t e = esp_wifi_set_mode(WIFI_MODE_STA);
  if (e != ESP_OK) ESP_LOGE(TAG, "set mode STA: %s", esp_err_to_name(e));
  ap_active_ = false;
  phase_ = Phase::StaUp;
  ESP_LOGI(TAG, "STA stable for 60 s: access point dropped");
}

}  // namespace bbfw
