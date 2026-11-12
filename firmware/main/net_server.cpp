#include "net_server.h"

#include <strings.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <optional>
#include <string>
#include <utility>

#include "app_state.h"
#include "bb/auth.h"
#include "bb/snapshot.h"
#include "cJSON.h"
#include "config_store.h"
#include "esp_log.h"
#include "esp_system.h"
#include "sdkconfig.h"

#if !CONFIG_HTTPD_WS_SUPPORT
#error "CONFIG_HTTPD_WS_SUPPORT must be enabled (see sdkconfig.defaults)"
#endif

// Embedded by EMBED_TXTFILES "web/manual.html" (a trailing NUL is appended).
extern const uint8_t manual_html_start[] asm("_binary_manual_html_start");
extern const uint8_t manual_html_end[] asm("_binary_manual_html_end");

namespace bbfw {

namespace {
const char* const TAG = "bb_net";

const char* status_line(int code) {
  switch (code) {
    case 200: return "200 OK";
    case 400: return "400 Bad Request";
    case 401: return "401 Unauthorized";
    case 404: return "404 Not Found";
    case 409: return "409 Conflict";
    case 413: return "413 Payload Too Large";
    default: return "500 Internal Server Error";
  }
}

constexpr const char kUnauthorized[] = "{\"error\":\"unauthorized\"}";
}  // namespace

NetServer::NetServer(SemaphoreHandle_t mu, bb::Board& board, bb::ControlHub& hub,
                     std::string token)
    : mu_(mu), board_(board), hub_(hub), token_(std::move(token)) {}

esp_err_t NetServer::start() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.stack_size = 10240;
  cfg.max_uri_handlers = 12;
  cfg.max_open_sockets = 10;  // LWIP_MAX_SOCKETS (16) - 3 internal
  cfg.lru_purge_enable = true;  // a dead half-open client must never block a new one
  cfg.recv_wait_timeout = 5;
  cfg.send_wait_timeout = 5;
  cfg.close_fn = &NetServer::close_cb;
  cfg.global_user_ctx = this;

  esp_err_t e = httpd_start(&server_, &cfg);
  if (e != ESP_OK) {
    ESP_LOGE(TAG, "httpd_start: %s", esp_err_to_name(e));
    return e;
  }

  struct Route {
    const char* uri;
    httpd_method_t method;
    esp_err_t (*fn)(httpd_req_t*);
    bool ws;
  };
  const Route routes[] = {
      {"/ws", HTTP_GET, &NetServer::h_ws, true},
      {"/state", HTTP_POST, &NetServer::h_state, false},
      {"/status", HTTP_GET, &NetServer::h_status, false},
      {"/manual/cmd", HTTP_POST, &NetServer::h_manual_cmd, false},
      {"/manual/state", HTTP_GET, &NetServer::h_manual_state, false},
      {"/config", HTTP_GET, &NetServer::h_config_get, false},
      {"/config", HTTP_POST, &NetServer::h_config_post, false},
      {"/", HTTP_GET, &NetServer::h_root, false},
  };
  for (const Route& r : routes) {
    httpd_uri_t u = {};
    u.uri = r.uri;
    u.method = r.method;
    u.handler = r.fn;
    u.user_ctx = this;
    u.is_websocket = r.ws;
    u.handle_control_frames = false;  // httpd answers ping/close itself
    u.supported_subprotocol = nullptr;
    e = httpd_register_uri_handler(server_, &u);
    if (e != ESP_OK) {
      ESP_LOGE(TAG, "register %s: %s", r.uri, esp_err_to_name(e));
      return e;
    }
  }

  esp_timer_create_args_t ta = {};
  ta.callback = &NetServer::tick_timer_cb;
  ta.arg = this;
  ta.dispatch_method = ESP_TIMER_TASK;
  ta.name = "bb_tick";
  ta.skip_unhandled_events = true;
  e = esp_timer_create(&ta, &tick_timer_);
  if (e != ESP_OK) return e;
  e = esp_timer_start_periodic(tick_timer_, 200 * 1000);
  if (e != ESP_OK) return e;

  ESP_LOGI(TAG, "HTTP/WS server listening on :80");
  return ESP_OK;
}

// ---- trampolines -----------------------------------------------------------------------

#define BB_SELF(r) static_cast<NetServer*>((r)->user_ctx)
esp_err_t NetServer::h_ws(httpd_req_t* r) { return BB_SELF(r)->handle_ws(r); }
esp_err_t NetServer::h_state(httpd_req_t* r) { return BB_SELF(r)->handle_hub_post(r, HubPath::State); }
esp_err_t NetServer::h_manual_cmd(httpd_req_t* r) {
  return BB_SELF(r)->handle_hub_post(r, HubPath::ManualCmd);
}
esp_err_t NetServer::h_manual_state(httpd_req_t* r) { return BB_SELF(r)->handle_manual_state(r); }
esp_err_t NetServer::h_config_get(httpd_req_t* r) { return BB_SELF(r)->handle_config_get(r); }
esp_err_t NetServer::h_config_post(httpd_req_t* r) { return BB_SELF(r)->handle_config_post(r); }
#undef BB_SELF

esp_err_t NetServer::h_status(httpd_req_t* r) {
  auto* self = static_cast<NetServer*>(r->user_ctx);
  std::string body;
  {
    MutexLock l(self->mu_);
    body = self->hub_.status_json();
  }
  send_json(r, 200, body);
  return ESP_OK;
}

esp_err_t NetServer::h_root(httpd_req_t* r) {
  const size_t len = static_cast<size_t>(manual_html_end - manual_html_start) - 1;  // minus NUL
  httpd_resp_set_type(r, "text/html");
  httpd_resp_set_hdr(r, "Cache-Control", "no-store");
  return httpd_resp_send(r, reinterpret_cast<const char*>(manual_html_start), len);
}

// ---- HTTP helpers ----------------------------------------------------------------------

const char* NetServer::auth_header(httpd_req_t* r, char* buf, size_t cap) {
  const size_t n = httpd_req_get_hdr_value_len(r, "Authorization");
  if (n == 0 || n >= cap) return nullptr;  // missing, or absurdly long: treated as no auth
  if (httpd_req_get_hdr_value_str(r, "Authorization", buf, cap) != ESP_OK) return nullptr;
  return buf;
}

bool NetServer::authorized(const char* auth_hdr) const {
  static const char kPrefix[] = "Bearer ";
  if (!auth_hdr || strncasecmp(auth_hdr, kPrefix, sizeof(kPrefix) - 1) != 0) return false;
  return bb::token_equal(token_.c_str(), auth_hdr + sizeof(kPrefix) - 1);
}

// Reads min(content_len, cap) bytes and NUL-terminates (buf needs cap + 1 bytes).
esp_err_t NetServer::read_body(httpd_req_t* r, char* buf, size_t cap, size_t& len) {
  const size_t want = r->content_len < cap ? r->content_len : cap;
  size_t got = 0;
  int timeouts = 0;
  while (got < want) {
    const int n = httpd_req_recv(r, buf + got, want - got);
    if (n == HTTPD_SOCK_ERR_TIMEOUT) {
      if (++timeouts > 3) return ESP_FAIL;
      continue;
    }
    if (n <= 0) return ESP_FAIL;
    got += static_cast<size_t>(n);
  }
  buf[got] = '\0';
  len = got;
  return ESP_OK;
}

void NetServer::send_json(httpd_req_t* r, int status, const char* body, size_t len) {
  httpd_resp_set_status(r, status_line(status));
  httpd_resp_set_type(r, "application/json");
  httpd_resp_set_hdr(r, "Cache-Control", "no-store");
  httpd_resp_send(r, body, len);
}

// ---- POST /state and POST /manual/cmd --------------------------------------------------

esp_err_t NetServer::handle_hub_post(httpd_req_t* r, HubPath which) {
  char auth[128];
  const char* ah = auth_header(r, auth, sizeof(auth));

  // Read at most 1024 + 1 bytes: one byte over the limit is enough for the core to answer
  // too_large / 413 without buffering an arbitrarily large body.
  constexpr size_t kCap = bb::kMaxMessageBytes + 1;
  char body[kCap + 1];
  size_t len = 0;
  if (read_body(r, body, kCap, len) != ESP_OK) return ESP_FAIL;
  const bool truncated = r->content_len > kCap;

  bb::HttpReply rep;
  {
    MutexLock l(mu_);
    rep = (which == HubPath::State) ? hub_.on_http_state(ah, body, len)
                                    : hub_.on_http_manual_cmd(ah, body, len);
  }
  send_json(r, rep.status, rep.body);
  if (truncated) {
    // Unread request body remains on the socket: do not keep-alive.
    httpd_sess_trigger_close(server_, httpd_req_to_sockfd(r));
  }
  return ESP_OK;
}

// ---- GET /manual/state -----------------------------------------------------------------

esp_err_t NetServer::handle_manual_state(httpd_req_t* r) {
  char auth[128];
  if (!authorized(auth_header(r, auth, sizeof(auth)))) {
    send_json(r, 401, kUnauthorized, sizeof(kUnauthorized) - 1);
    return ESP_OK;
  }
  bb::Snapshot s;
  {
    MutexLock l(mu_);
    s = board_.manual().current();
  }
  char buf[bb::kMaxMessageBytes + 1];
  const size_t n = bb::snapshot_to_json(s, buf, sizeof(buf));
  if (n == 0) {
    static const char kErr[] = "{\"error\":\"internal\"}";
    send_json(r, 500, kErr, sizeof(kErr) - 1);
    return ESP_OK;
  }
  send_json(r, 200, buf, n);
  return ESP_OK;
}

// ---- GET/POST /config ------------------------------------------------------------------

esp_err_t NetServer::handle_config_get(httpd_req_t* r) {
  char auth[128];
  if (!authorized(auth_header(r, auth, sizeof(auth)))) {
    send_json(r, 401, kUnauthorized, sizeof(kUnauthorized) - 1);
    return ESP_OK;
  }
  Config c;
  config_read(c);  // current NVS contents (may differ from the running config until reboot)

  cJSON* o = cJSON_CreateObject();
  if (!o) return ESP_FAIL;
  cJSON_AddStringToObject(o, "ssid", c.ssid.c_str());
  cJSON_AddStringToObject(o, "host", c.host.c_str());
  cJSON_AddNumberToObject(o, "stale_ms", c.stale_ms);
  cJSON_AddBoolToObject(o, "manual_en", c.manual_en);
  // Secrets are never echoed, only whether they are set.
  cJSON_AddBoolToObject(o, "pass_set", !c.pass.empty());
  cJSON_AddBoolToObject(o, "ap_pass_set", !c.ap_pass.empty());
  cJSON_AddBoolToObject(o, "token_set", !c.token.empty());
  cJSON_AddStringToObject(o, "applies", "on_reboot");
  char* text = cJSON_PrintUnformatted(o);
  cJSON_Delete(o);
  if (!text) return ESP_FAIL;
  send_json(r, 200, text, strlen(text));
  cJSON_free(text);
  return ESP_OK;
}

esp_err_t NetServer::handle_config_post(httpd_req_t* r) {
  char auth[128];
  if (!authorized(auth_header(r, auth, sizeof(auth)))) {
    send_json(r, 401, kUnauthorized, sizeof(kUnauthorized) - 1);
    return ESP_OK;
  }
  if (r->content_len > kConfigMaxBody) {
    static const char kBig[] = "{\"result\":\"invalid\",\"error\":\"too_large\"}";
    send_json(r, 413, kBig, sizeof(kBig) - 1);
    httpd_sess_trigger_close(server_, httpd_req_to_sockfd(r));
    return ESP_OK;
  }
  char body[kConfigMaxBody + 1];
  size_t len = 0;
  if (read_body(r, body, kConfigMaxBody, len) != ESP_OK) return ESP_FAIL;

  auto reject = [&](const char* error, const char* field) {
    char msg[128];
    int n = snprintf(msg, sizeof(msg), "{\"result\":\"invalid\",\"error\":\"%s\",\"field\":\"%s\"}",
                     error, field);
    send_json(r, 400, msg, n > 0 ? static_cast<size_t>(n) : 0);
    return ESP_OK;
  };

  cJSON* root = cJSON_ParseWithLength(body, len);
  if (!root || !cJSON_IsObject(root)) {
    cJSON_Delete(root);
    return reject("not_json", "");
  }

  ConfigUpdate u;
  bool reboot = false;
  const char* bad = nullptr;
  auto str = [&](const char* k, std::optional<std::string>& dst) {
    cJSON* it = cJSON_GetObjectItemCaseSensitive(root, k);
    if (!it) return;
    if (!cJSON_IsString(it) || !it->valuestring) {
      if (!bad) bad = k;
      return;
    }
    dst = std::string(it->valuestring);
  };
  str("ssid", u.ssid);
  str("pass", u.pass);
  str("token", u.token);
  str("host", u.host);
  str("ap_pass", u.ap_pass);
  if (cJSON* it = cJSON_GetObjectItemCaseSensitive(root, "stale_ms")) {
    const double d = cJSON_IsNumber(it) ? it->valuedouble : 0;
    if (!cJSON_IsNumber(it) || d < -2e9 || d > 2e9 || d != static_cast<double>(static_cast<int32_t>(d))) {
      if (!bad) bad = "stale_ms";
    } else {
      u.stale_ms = static_cast<int32_t>(d);
    }
  }
  if (cJSON* it = cJSON_GetObjectItemCaseSensitive(root, "manual_en")) {
    if (!cJSON_IsBool(it)) {
      if (!bad) bad = "manual_en";
    } else {
      u.manual_en = cJSON_IsTrue(it);
    }
  }
  if (cJSON* it = cJSON_GetObjectItemCaseSensitive(root, "reboot")) {
    if (!cJSON_IsBool(it)) {
      if (!bad) bad = "reboot";
    } else {
      reboot = cJSON_IsTrue(it);
    }
  }
  cJSON_Delete(root);

  if (bad) return reject("bad_type", bad);
  if (const char* f = config_validate(u)) return reject("out_of_range", f);

  const esp_err_t se = config_save(u);
  if (se != ESP_OK) {
    ESP_LOGE(TAG, "config_save: %s", esp_err_to_name(se));
    static const char kErr[] = "{\"result\":\"error\",\"error\":\"storage\"}";
    send_json(r, 500, kErr, sizeof(kErr) - 1);
    return ESP_OK;
  }
  ESP_LOGI(TAG, "config saved%s", reboot ? ", rebooting" : " (applies on reboot)");
  char msg[96];
  const int n = snprintf(msg, sizeof(msg), "{\"result\":\"saved\",\"reboot_required\":true,\"rebooting\":%s}",
                         reboot ? "true" : "false");
  send_json(r, 200, msg, n > 0 ? static_cast<size_t>(n) : 0);
  if (reboot) schedule_reboot();
  return ESP_OK;
}

void NetServer::reboot_timer_cb(void*) { esp_restart(); }

void NetServer::schedule_reboot() {
  if (!reboot_timer_) {
    esp_timer_create_args_t a = {};
    a.callback = &NetServer::reboot_timer_cb;
    a.arg = this;
    a.dispatch_method = ESP_TIMER_TASK;
    a.name = "bb_reboot";
    if (esp_timer_create(&a, &reboot_timer_) != ESP_OK) return;
  }
  esp_timer_start_once(reboot_timer_, 1000 * 1000);  // let the HTTP response flush first
}

// ---- WebSocket -------------------------------------------------------------------------

NetServer::WsSlot* NetServer::ws_find_fd(int fd) {
  for (auto& s : ws_) {
    if (s.fd == fd) return &s;
  }
  return nullptr;
}

NetServer::WsSlot* NetServer::ws_find_conn(bb::ConnId c) {
  for (auto& s : ws_) {
    if (s.fd >= 0 && s.conn == c) return &s;
  }
  return nullptr;
}

void NetServer::ws_open(int fd) {
  if (WsSlot* old = ws_find_fd(fd)) {  // fd reuse without a close hook: should not happen
    ws_closed(old->fd);
  }
  WsSlot* free_slot = ws_find_fd(-1);
  if (!free_slot) {
    ESP_LOGW(TAG, "ws: no free session slot, closing fd %d", fd);
    httpd_sess_trigger_close(server_, fd);
    return;
  }
  free_slot->fd = fd;
  free_slot->conn = next_conn_++;
  bool accepted;
  {
    MutexLock l(mu_);
    accepted = hub_.on_open(free_slot->conn);
  }
  if (!accepted) {
    ESP_LOGW(TAG, "ws: too many pending connections, closing fd %d", fd);
    httpd_sess_trigger_close(server_, fd);  // close_cb -> ws_closed -> hub.on_close (no-op)
  }
}

void NetServer::ws_closed(int fd) {
  WsSlot* s = ws_find_fd(fd);
  if (!s) return;
  const bb::ConnId c = s->conn;
  s->fd = -1;
  s->conn = 0;
  MutexLock l(mu_);
  hub_.on_close(c);
}

void NetServer::close_cb(httpd_handle_t hd, int sockfd) {
  auto* self = static_cast<NetServer*>(httpd_get_global_user_ctx(hd));
  if (self && sockfd >= 0 && self->ws_find_fd(sockfd)) self->ws_closed(sockfd);
  close(sockfd);  // a custom close_fn replaces the default one: it must close the socket
}

esp_err_t NetServer::handle_ws(httpd_req_t* r) {
  const int fd = httpd_req_to_sockfd(r);
  if (r->method == HTTP_GET) {  // upgrade handshake completed
    ws_open(fd);
    return ESP_OK;
  }

  httpd_ws_frame_t pkt = {};
  esp_err_t e = httpd_ws_recv_frame(r, &pkt, 0);  // length only
  if (e != ESP_OK) return e;
  if (pkt.len > kWsMaxFrame) {
    ESP_LOGW(TAG, "ws: frame of %u bytes too large, closing", static_cast<unsigned>(pkt.len));
    return ESP_FAIL;
  }
  if (pkt.len > 0) {
    pkt.payload = ws_buf_;
    e = httpd_ws_recv_frame(r, &pkt, pkt.len);
    if (e != ESP_OK) return e;
  }
  ws_buf_[pkt.len] = '\0';

  if (pkt.type == HTTPD_WS_TYPE_CONTINUE || !pkt.final) {
    ESP_LOGW(TAG, "ws: fragmented messages unsupported, closing");
    return ESP_FAIL;
  }
  if (pkt.type != HTTPD_WS_TYPE_TEXT) return ESP_OK;  // binary etc.: ignored

  WsSlot* s = ws_find_fd(fd);
  if (!s) {  // handshake was not seen (cannot normally happen)
    ws_open(fd);
    s = ws_find_fd(fd);
    if (!s) return ESP_FAIL;
  }
  std::vector<bb::Outbound> out;
  {
    MutexLock l(mu_);
    hub_.on_text(s->conn, reinterpret_cast<const char*>(ws_buf_), pkt.len, out);
  }
  send_outbound(out);
  return ESP_OK;
}

void NetServer::send_outbound(const std::vector<bb::Outbound>& out) {
  for (const bb::Outbound& o : out) {
    WsSlot* s = ws_find_conn(o.conn);
    if (!s) continue;  // connection already gone
    const int fd = s->fd;

    httpd_ws_frame_t f = {};
    f.type = HTTPD_WS_TYPE_TEXT;
    f.final = true;
    f.payload = reinterpret_cast<uint8_t*>(const_cast<char*>(o.text.data()));
    f.len = o.text.size();
    const esp_err_t e = httpd_ws_send_frame_async(server_, fd, &f);
    if (e != ESP_OK) {
      ESP_LOGW(TAG, "ws send failed on fd %d: %s", fd, esp_err_to_name(e));
      httpd_sess_trigger_close(server_, fd);
      continue;
    }
    if (o.close_after) {
      httpd_ws_frame_t cf = {};
      cf.type = HTTPD_WS_TYPE_CLOSE;
      cf.final = true;
      httpd_ws_send_frame_async(server_, fd, &cf);  // best effort
      httpd_sess_trigger_close(server_, fd);        // close_cb -> hub.on_close
    }
  }
}

// ---- 200 ms tick -----------------------------------------------------------------------

void NetServer::tick_timer_cb(void* arg) {
  auto* self = static_cast<NetServer*>(arg);
  if (self->tick_pending_.exchange(true)) return;  // previous tick not yet executed
  if (httpd_queue_work(self->server_, &NetServer::tick_work, self) != ESP_OK) {
    self->tick_pending_ = false;
  }
}

void NetServer::tick_work(void* arg) {
  auto* self = static_cast<NetServer*>(arg);
  self->tick_pending_ = false;
  self->do_tick();
}

void NetServer::do_tick() {
  std::vector<bb::Outbound> out;
  {
    MutexLock l(mu_);
    hub_.tick(out);
  }
  send_outbound(out);
}

}  // namespace bbfw
