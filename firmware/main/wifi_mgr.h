// Wi-Fi manager: STA first, APSTA fallback (docs/architecture.md "Wi-Fi / mDNS / config").
//  - STA to the configured SSID: gives up after 15 s or 5 failed attempts.
//  - Fallback: AP "BracketBoard-XXXX" (WPA2, 192.168.4.1) + STA retry every 30 s.
//  - AP is dropped once STA has been connected for 60 s.
// All state transitions run on one internal task; events are forwarded through a queue.
#pragma once
#include <atomic>
#include <cstdint>
#include <string>
#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

namespace bbfw {

struct WifiSettings {
  std::string ssid;     // empty => AP-only setup mode
  std::string pass;     // empty with ssid => open network
  std::string ap_pass;  // WPA2, 8-63 chars (validated by config_store)
  std::string host;     // DHCP/mDNS hostname for the STA interface
};

class WifiMgr {
 public:
  // Creates netifs, initialises Wi-Fi, starts the manager task. Non-blocking.
  esp_err_t start(const WifiSettings& s);

  bool sta_connected() const { return sta_connected_.load(); }
  bool ap_active() const { return ap_active_.load(); }

 private:
  enum class Phase : uint8_t { StaConnecting, StaUp, ApFallback };
  enum class EvType : uint8_t { StaDisconnected, StaGotIp };
  struct Ev {
    EvType type;
    uint8_t reason;
  };

  static void on_event(void* arg, esp_event_base_t base, int32_t id, void* data);
  static void task_entry(void* arg);
  void task();
  void handle_event(const Ev& ev, int64_t now_us);
  void poll_timers(int64_t now_us);
  void enter_fallback(int64_t now_us);
  void leave_fallback();
  void connect_sta(const char* why);

  WifiSettings s_;
  bool have_ssid_ = false;
  QueueHandle_t q_ = nullptr;
  esp_netif_t* sta_netif_ = nullptr;
  esp_netif_t* ap_netif_ = nullptr;
  char ap_ssid_[24] = {0};

  // Owned by the manager task after start().
  Phase phase_ = Phase::StaConnecting;
  int retries_ = 0;
  int64_t attempt_start_us_ = 0;
  int64_t connected_since_us_ = 0;  // 0 = not connected
  int64_t last_retry_us_ = 0;

  std::atomic<bool> sta_connected_{false};
  std::atomic<bool> ap_active_{false};
};

}  // namespace bbfw
