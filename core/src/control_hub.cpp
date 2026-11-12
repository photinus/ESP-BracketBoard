// ControlHub: WS hello/auth/single-client state machine + HTTP handlers.
// See docs/protocol-v1.md sections 3 and 4.
//
// Documented behaviours:
//  - Connections that emit a close_after frame are forgotten by the hub at that moment
//    (a later on_close for them is a harmless no-op, and further text from them is ignored).
//  - on_open beyond max_pending pending connections is not tracked at all: the hub emits
//    nothing for it and ignores every frame it sends (the transport should drop it).
//    Re-opening an id the hub already knows replaces the old record (the old socket must be gone).
//  - Hello handling order: size/JSON/type -> bad_message; v != 1 -> unsupported_version;
//    token -> auth_failed.
//  - A control client's frame that is not parseable JSON, or whose "type" is not "state",
//    gets error bad_message and stays connected. Frames over kMaxMessageBytes are handed to
//    the board as state (-> ack invalid/too_large) without being parsed here.
//  - Ack for an invalid state carries seq 0 (nothing trustworthy was parsed).
//  - Strings are emitted through cJSON, which escapes them; the token is never output.
#include "bb/control_hub.h"
#include <cctype>
#include <memory>
#include <cstring>
#include "bb/auth.h"
#include "cJSON.h"

namespace bb {

namespace {

struct JsonDeleter {
  void operator()(cJSON* j) const { cJSON_Delete(j); }
};
using JsonPtr = std::unique_ptr<cJSON, JsonDeleter>;

std::string print(cJSON* obj) {
  char* s = cJSON_PrintUnformatted(obj);
  std::string r = s ? s : "{}";
  if (s) cJSON_free(s);
  return r;
}

std::string error_json(const char* code) {
  JsonPtr o(cJSON_CreateObject());
  cJSON_AddStringToObject(o.get(), "type", "error");
  cJSON_AddStringToObject(o.get(), "code", code);
  return print(o.get());
}

const char* result_name(IngestResult::Kind k) {
  switch (k) {
    case IngestResult::Applied: return "applied";
    case IngestResult::Duplicate: return "duplicate";
    case IngestResult::Stale: return "stale";
    case IngestResult::Invalid: return "invalid";
    case IngestResult::ManualLocked: return "manual_locked";
  }
  return "invalid";
}

std::string ack_json(const IngestResult& r) {
  JsonPtr o(cJSON_CreateObject());
  cJSON_AddStringToObject(o.get(), "type", "ack");
  cJSON_AddNumberToObject(o.get(), "seq", static_cast<double>(r.seq));
  cJSON_AddStringToObject(o.get(), "result", result_name(r.kind));
  if (r.kind == IngestResult::Invalid) {
    cJSON_AddStringToObject(o.get(), "error", parse_error_code(r.parse.err));
    if (r.parse.field) cJSON_AddStringToObject(o.get(), "field", r.parse.field);
  }
  return print(o.get());
}

std::string simple_json(const char* key, const char* value) {
  JsonPtr o(cJSON_CreateObject());
  cJSON_AddStringToObject(o.get(), key, value);
  return print(o.get());
}

// "Bearer <token>" with a case-insensitive scheme. Returns true if the token matches.
bool bearer_ok(const std::string& token, const char* hdr) {
  if (!hdr) return false;
  static const char kScheme[] = "bearer";
  size_t i = 0;
  for (; kScheme[i]; ++i) {
    if (!hdr[i] || std::tolower(static_cast<unsigned char>(hdr[i])) != kScheme[i]) return false;
  }
  if (hdr[i] != ' ') return false;
  while (hdr[i] == ' ') ++i;
  return token_equal(token.c_str(), hdr + i);
}

}  // namespace

ControlHub::ControlHub(Board& board, const TimeSource& ts, std::string token,
                       int hello_timeout_ms, size_t max_pending, HubInfo info)
    : board_(board),
      ts_(ts),
      token_(std::move(token)),
      hello_timeout_ms_(hello_timeout_ms),
      max_pending_(max_pending),
      info_(std::move(info)),
      boot_us_(ts.now_us()) {}

void ControlHub::on_open(ConnId c) {
  // A reused id means the old socket is gone: drop whatever we knew about it.
  on_close(c);
  if (pending_.size() >= max_pending_) return;  // not tracked; its frames are ignored
  pending_.push_back({c, ts_.now_us()});
}

void ControlHub::on_close(ConnId c) {
  for (size_t i = 0; i < pending_.size(); ++i) {
    if (pending_[i].id == c) {
      pending_.erase(pending_.begin() + static_cast<std::ptrdiff_t>(i));
      break;
    }
  }
  if (client_ && *client_ == c) client_.reset();
}

void ControlHub::tick(std::vector<Outbound>& out) {
  const int64_t limit = static_cast<int64_t>(hello_timeout_ms_) * 1000;
  const int64_t now = ts_.now_us();
  for (size_t i = 0; i < pending_.size();) {
    if (now - pending_[i].opened_us > limit) {
      out.push_back({pending_[i].id, error_json("hello_timeout"), true});
      pending_.erase(pending_.begin() + static_cast<std::ptrdiff_t>(i));
    } else {
      ++i;
    }
  }
}

void ControlHub::on_text(ConnId c, const char* data, size_t len, std::vector<Outbound>& out) {
  size_t pi = pending_.size();
  for (size_t i = 0; i < pending_.size(); ++i) {
    if (pending_[i].id == c) { pi = i; break; }
  }

  if (pi < pending_.size()) {
    // ---- pending: first frame must be a valid hello ----
    auto reject = [&](const char* code) {
      out.push_back({c, error_json(code), true});
      pending_.erase(pending_.begin() + static_cast<std::ptrdiff_t>(pi));
    };
    if (!data || len > kMaxMessageBytes) return reject("bad_message");
    JsonPtr j(cJSON_ParseWithLength(data, len));
    if (!j || !cJSON_IsObject(j.get())) return reject("bad_message");
    const cJSON* type = cJSON_GetObjectItemCaseSensitive(j.get(), "type");
    if (!cJSON_IsString(type) || !type->valuestring || std::strcmp(type->valuestring, "hello") != 0)
      return reject("bad_message");
    const cJSON* v = cJSON_GetObjectItemCaseSensitive(j.get(), "v");
    if (!cJSON_IsNumber(v)) return reject("bad_message");
    if (v->valuedouble != static_cast<double>(kProtocolVersion)) return reject("unsupported_version");
    const cJSON* tok = cJSON_GetObjectItemCaseSensitive(j.get(), "token");
    if (!cJSON_IsString(tok) || !tok->valuestring || !token_equal(token_.c_str(), tok->valuestring))
      return reject("auth_failed");

    // Authenticated: becomes the control client, replacing any previous one.
    pending_.erase(pending_.begin() + static_cast<std::ptrdiff_t>(pi));
    if (client_ && *client_ != c) out.push_back({*client_, error_json("replaced"), true});
    client_ = c;

    JsonPtr ack(cJSON_CreateObject());
    cJSON_AddStringToObject(ack.get(), "type", "hello_ack");
    const int supported[] = {kProtocolVersion};
    cJSON_AddItemToObject(ack.get(), "supported_v", cJSON_CreateIntArray(supported, 1));
    cJSON_AddStringToObject(ack.get(), "device", info_.device.c_str());
    cJSON_AddStringToObject(ack.get(), "fw", info_.fw.c_str());
    cJSON_AddStringToObject(ack.get(), "mode", board_.manual_active() ? "manual" : "agent");
    if (board_.seq().has_last()) {
      cJSON_AddStringToObject(ack.get(), "session", board_.seq().session());
      cJSON_AddNumberToObject(ack.get(), "last_seq", static_cast<double>(board_.seq().last_seq()));
    } else {
      cJSON_AddNullToObject(ack.get(), "session");
      cJSON_AddNullToObject(ack.get(), "last_seq");
    }
    out.push_back({c, print(ack.get()), false});
    return;
  }

  if (client_ && *client_ == c) {
    // ---- control client ----
    bool is_state = len > kMaxMessageBytes;  // let the board reject it as too_large
    if (!is_state && data) {
      JsonPtr j(cJSON_ParseWithLength(data, len));
      if (j && cJSON_IsObject(j.get())) {
        const cJSON* type = cJSON_GetObjectItemCaseSensitive(j.get(), "type");
        is_state = cJSON_IsString(type) && type->valuestring &&
                   std::strcmp(type->valuestring, "state") == 0;
      }
    }
    if (!is_state) {
      out.push_back({c, error_json("bad_message"), false});
      return;
    }
    const IngestResult r = board_.ingest_state(data ? data : "", len, Source::Ws);
    out.push_back({c, ack_json(r), false});
    return;
  }
  // unknown connection: ignored
}

HttpReply ControlHub::on_http_state(const char* authorization_hdr, const char* body, size_t len) {
  HttpReply rep;
  if (!bearer_ok(token_, authorization_hdr)) {
    rep.status = 401;
    rep.body = simple_json("error", "unauthorized");
    return rep;
  }
  if (len > kMaxMessageBytes) {
    IngestResult r;
    r.kind = IngestResult::Invalid;
    r.parse.err = ParseError::TooLarge;
    rep.status = 413;
    rep.body = ack_json(r);
    return rep;
  }
  const IngestResult r = board_.ingest_state(body ? body : "", len, Source::Http);
  rep.status = r.kind == IngestResult::Invalid ? 400 : 200;
  rep.body = ack_json(r);
  return rep;
}

HttpReply ControlHub::on_http_manual_cmd(const char* authorization_hdr, const char* body,
                                         size_t len) {
  HttpReply rep;
  if (!bearer_ok(token_, authorization_hdr)) {
    rep.status = 401;
    rep.body = simple_json("error", "unauthorized");
    return rep;
  }
  if (!board_.manual_active()) {
    rep.status = 409;
    rep.body = simple_json("result", "manual_locked");
    return rep;
  }
  if (len > kMaxMessageBytes || !board_.manual().apply(body ? body : "", len)) {
    rep.status = 400;
    rep.body = simple_json("result", "invalid");
    return rep;
  }
  rep.status = 200;
  rep.body = simple_json("result", "applied");
  return rep;
}

std::string ControlHub::status_json() const {
  JsonPtr o(cJSON_CreateObject());
  cJSON_AddStringToObject(o.get(), "device", info_.device.c_str());
  cJSON_AddStringToObject(o.get(), "fw", info_.fw.c_str());
  cJSON_AddStringToObject(o.get(), "mode", board_.manual_active() ? "manual" : "agent");
  cJSON_AddBoolToObject(o.get(), "agent_live", board_.agent_live());
  if (board_.seq().has_last()) {
    cJSON_AddStringToObject(o.get(), "session", board_.seq().session());
    cJSON_AddNumberToObject(o.get(), "last_seq", static_cast<double>(board_.seq().last_seq()));
  } else {
    cJSON_AddNullToObject(o.get(), "session");
    cJSON_AddNullToObject(o.get(), "last_seq");
  }
  const int64_t up = (ts_.now_us() - boot_us_) / 1000000;
  cJSON_AddNumberToObject(o.get(), "uptime_s", static_cast<double>(up < 0 ? 0 : up));
  return print(o.get());
}

}  // namespace bb
