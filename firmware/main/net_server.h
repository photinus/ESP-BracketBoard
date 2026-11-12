// HTTP + WebSocket front end. One esp_http_server instance on port 80.
//
//   GET  /              embedded manual.html
//   GET  /ws            WebSocket (ControlHub hello/auth/state protocol)
//   POST /state         HTTP fallback snapshot (Bearer)
//   GET  /status        status JSON (no auth, never contains the token)
//   POST /manual/cmd    manual-mode command (Bearer)
//   GET  /manual/state  manual snapshot as a state message (Bearer)
//   GET/POST /config    NVS-backed settings (Bearer); applied on reboot
//
// Threading: every handler, the httpd close hook and the 200 ms tick work item run on the
// single httpd task, so the fd->ConnId table needs no lock. Board+ControlHub are guarded by
// the shared mutex; Outbound frames are always sent AFTER the mutex has been released.
#pragma once
#include <atomic>
#include <string>
#include <vector>

#include "bb/board.h"
#include "bb/control_hub.h"
#include "esp_err.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

namespace bbfw {

class NetServer {
 public:
  NetServer(SemaphoreHandle_t mu, bb::Board& board, bb::ControlHub& hub, std::string token);

  esp_err_t start();

 private:
  static constexpr size_t kMaxWs = 8;         // tracked WebSocket sessions
  static constexpr size_t kWsMaxFrame = 2048; // larger frames close the connection
  static constexpr size_t kConfigMaxBody = 512;

  struct WsSlot {
    int fd = -1;
    bb::ConnId conn = 0;
  };

  // httpd trampolines
  static esp_err_t h_ws(httpd_req_t* r);
  static esp_err_t h_state(httpd_req_t* r);
  static esp_err_t h_status(httpd_req_t* r);
  static esp_err_t h_manual_cmd(httpd_req_t* r);
  static esp_err_t h_manual_state(httpd_req_t* r);
  static esp_err_t h_root(httpd_req_t* r);
  static esp_err_t h_config_get(httpd_req_t* r);
  static esp_err_t h_config_post(httpd_req_t* r);
  static void close_cb(httpd_handle_t hd, int sockfd);
  static void tick_work(void* arg);
  static void tick_timer_cb(void* arg);
  static void reboot_timer_cb(void* arg);

  esp_err_t handle_ws(httpd_req_t* r);
  esp_err_t handle_state(httpd_req_t* r);
  esp_err_t handle_manual_cmd(httpd_req_t* r);
  esp_err_t handle_manual_state(httpd_req_t* r);
  esp_err_t handle_config_get(httpd_req_t* r);
  esp_err_t handle_config_post(httpd_req_t* r);

  // Shared HTTP body of /state and /manual/cmd.
  enum class HubPath { State, ManualCmd };
  esp_err_t handle_hub_post(httpd_req_t* r, HubPath which);

  // WebSocket session bookkeeping (httpd task only).
  void ws_open(int fd);
  void ws_closed(int fd);
  WsSlot* ws_find_fd(int fd);
  WsSlot* ws_find_conn(bb::ConnId c);
  void send_outbound(const std::vector<bb::Outbound>& out);
  void do_tick();

  // HTTP helpers
  static const char* auth_header(httpd_req_t* r, char* buf, size_t cap);
  bool authorized(const char* auth_hdr) const;
  static esp_err_t read_body(httpd_req_t* r, char* buf, size_t cap, size_t& len);
  static void send_json(httpd_req_t* r, int status, const char* body, size_t len);
  static void send_json(httpd_req_t* r, int status, const std::string& body) {
    send_json(r, status, body.data(), body.size());
  }
  void schedule_reboot();

  SemaphoreHandle_t mu_;
  bb::Board& board_;
  bb::ControlHub& hub_;
  const std::string token_;
  httpd_handle_t server_ = nullptr;
  esp_timer_handle_t tick_timer_ = nullptr;
  esp_timer_handle_t reboot_timer_ = nullptr;
  std::atomic<bool> tick_pending_{false};

  WsSlot ws_[kMaxWs];
  bb::ConnId next_conn_ = 1;
  uint8_t ws_buf_[kWsMaxFrame + 1];
};

}  // namespace bbfw
