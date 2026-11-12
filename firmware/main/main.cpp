// BracketBoard firmware entry point: wires config, Wi-Fi, mDNS, the core (Board +
// ControlHub), the HTTP/WS server and the 20 Hz render task.
#include <cstdio>
#include <cstdlib>

#include "app_state.h"
#include "bb/board.h"
#include "bb/control_hub.h"
#include "config_store.h"
#include "esp_log.h"
#include "esp_time_source.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mdns_svc.h"
#include "net_server.h"
#include "panel_display.h"
#include "wifi_mgr.h"

namespace {

const char* const TAG = "bb_main";
constexpr int64_t kSetupOverlayUs = 60LL * 1000 * 1000;

struct RenderCtx {
  SemaphoreHandle_t mu;
  bb::Board* board;
  bbfw::PanelDisplay* display;
  bbfw::WifiMgr* wifi;
  bool show_token_overlay;  // true only on the boot that generated the token
  char token[40];
};

// 20 Hz. Never blocks for long: the mutex wait is bounded and a missed frame simply
// re-renders the previous one.
void render_task(void* arg) {
  auto* c = static_cast<RenderCtx*>(arg);
  bb::Frame frame;  // default: splash / waiting
  TickType_t last_wake = xTaskGetTickCount();
  for (;;) {
    if (xSemaphoreTake(c->mu, pdMS_TO_TICKS(10)) == pdTRUE) {
      frame = c->board->frame();
      xSemaphoreGive(c->mu);
    }
    const bool overlay = c->show_token_overlay && c->wifi->ap_active() &&
                         esp_timer_get_time() < kSetupOverlayUs;
    c->display->set_setup_overlay(overlay ? c->token : nullptr);
    c->display->render(frame);
    vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(50));
  }
}

}  // namespace

extern "C" void app_main(void) {
  ESP_ERROR_CHECK(bbfw::config_init());

  static bbfw::Config cfg;
  bool token_generated = false;
  if (bbfw::config_load(cfg, token_generated) != ESP_OK) {
    ESP_LOGW(TAG, "config load/persist problem, continuing with what we have");
  }
  if (token_generated) {
    // The ONLY place the token is ever printed. Read it from the serial console.
    printf("\n==========================================\n"
           " BracketBoard first boot: control token\n"
           "   %s\n"
           " (stored in NVS; shown once. Use it for the agent, /manual and /config)\n"
           "==========================================\n\n",
           cfg.token.c_str());
  }

  static bbfw::EspTimeSource time_source;
  bb::BoardConfig bc;
  bc.clock.stale_timeout_ms = cfg.stale_ms;
  bc.manual_enabled = cfg.manual_en;  // default off
  static bb::Board board(time_source, bc);
  static bb::ControlHub hub(board, time_source, cfg.token);

  static bbfw::PanelDisplay display;
  display.begin();

  static SemaphoreHandle_t mu = xSemaphoreCreateMutex();
  if (!mu) {
    ESP_LOGE(TAG, "mutex alloc failed");
    abort();
  }

  static bbfw::WifiMgr wifi;
  bbfw::WifiSettings ws;
  ws.ssid = cfg.ssid;
  ws.pass = cfg.pass;
  ws.ap_pass = cfg.ap_pass;
  ws.host = cfg.host;
  ESP_ERROR_CHECK(wifi.start(ws));

  if (bbfw::mdns_svc_start(cfg.host) != ESP_OK) {
    ESP_LOGW(TAG, "mDNS unavailable; reach the sign by IP");  // not fatal
  }

  static bbfw::NetServer net(mu, board, hub, cfg.token);
  ESP_ERROR_CHECK(net.start());

  static RenderCtx rc;
  rc.mu = mu;
  rc.board = &board;
  rc.display = &display;
  rc.wifi = &wifi;
  rc.show_token_overlay = token_generated;
  snprintf(rc.token, sizeof(rc.token), "%s", cfg.token.c_str());
  if (xTaskCreate(render_task, "bb_render", 4096, &rc, 5, nullptr) != pdPASS) {
    ESP_LOGE(TAG, "render task create failed");
    abort();
  }

  ESP_LOGI(TAG, "up: host=%s.local manual=%s stale=%dms", cfg.host.c_str(),
           cfg.manual_en ? "enabled" : "off", static_cast<int>(cfg.stale_ms));
}
