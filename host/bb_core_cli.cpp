// Stdio bridge: drives a real Board + ControlHub from line-delimited JSON on stdin.
// Protocol: docs/architecture.md "Stdio bridge: bb_core_cli".
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <memory>
#include <string>
#include <vector>
#include "bb/board.h"
#include "bb/control_hub.h"
#include "bb/time_source.h"
#include "cJSON.h"
#include "steady_clock_source.h"

using namespace bb;

namespace {

struct JsonDeleter {
  void operator()(cJSON* j) const { cJSON_Delete(j); }
};
using JsonPtr = std::unique_ptr<cJSON, JsonDeleter>;

void emit(cJSON* obj) {
  char* s = cJSON_PrintUnformatted(obj);
  if (s) {
    std::fputs(s, stdout);
    cJSON_free(s);
  }
  std::fputc('\n', stdout);
  std::fflush(stdout);
  cJSON_Delete(obj);
}

void emit_done() { emit([] { cJSON* o = cJSON_CreateObject(); cJSON_AddStringToObject(o, "ch", "done"); return o; }()); }

void emit_error(const std::string& msg) {
  cJSON* o = cJSON_CreateObject();
  cJSON_AddStringToObject(o, "ch", "error");
  cJSON_AddStringToObject(o, "msg", msg.c_str());
  emit(o);
}

void emit_outbound(const std::vector<Outbound>& outs) {
  for (const Outbound& ob : outs) {
    cJSON* o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "ch", "ws");
    cJSON_AddNumberToObject(o, "conn", ob.conn);
    cJSON_AddStringToObject(o, "send", ob.text.c_str());
    cJSON_AddBoolToObject(o, "close", ob.close_after);
    emit(o);
  }
}

const char* str_of(const cJSON* obj, const char* key) {
  const cJSON* it = cJSON_GetObjectItemCaseSensitive(obj, key);
  return (cJSON_IsString(it) && it->valuestring) ? it->valuestring : nullptr;
}

bool int_of(const cJSON* obj, const char* key, long long& out) {
  const cJSON* it = cJSON_GetObjectItemCaseSensitive(obj, key);
  if (!cJSON_IsNumber(it)) return false;
  out = static_cast<long long>(it->valuedouble);
  return true;
}

cJSON* frame_json(const Frame& f) {
  cJSON* o = cJSON_CreateObject();
  cJSON_AddStringToObject(o, "screen", screen_name(f.screen));
  cJSON_AddStringToObject(o, "clock", f.clock);
  cJSON_AddBoolToObject(o, "colon_on", f.colon_on);
  cJSON_AddBoolToObject(o, "stale", f.stale);
  cJSON_AddNumberToObject(o, "left_score", f.left_score);
  cJSON_AddNumberToObject(o, "right_score", f.right_score);
  cJSON_AddNumberToObject(o, "left_warnings", f.left_warnings);
  cJSON_AddNumberToObject(o, "right_warnings", f.right_warnings);
  cJSON_AddNumberToObject(o, "period", f.period);
  cJSON_AddStringToObject(o, "clock_state", clock_state_name(f.clock_state));
  cJSON_AddStringToObject(o, "weight", f.weight);
  cJSON_AddStringToObject(o, "bout", f.bout);
  cJSON_AddBoolToObject(o, "colors_swapped", f.colors_swapped);
  cJSON_AddBoolToObject(o, "waiting", f.waiting);
  cJSON_AddBoolToObject(o, "manual", f.manual);
  return o;
}

}  // namespace

int main(int argc, char** argv) {
  std::string token;
  bool have_token = false, fake = false, manual = false;
  int stale_ms = 3000;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--token" && i + 1 < argc) { token = argv[++i]; have_token = true; }
    else if (a == "--stale-ms" && i + 1 < argc) { stale_ms = std::atoi(argv[++i]); }
    else if (a == "--fake-time") { fake = true; }
    else if (a == "--manual") { manual = true; }
    else {
      std::fprintf(stderr, "usage: bb_core_cli --token T [--stale-ms N] [--fake-time] [--manual]\n");
      return 2;
    }
  }
  if (!have_token || stale_ms <= 0) {
    std::fprintf(stderr, "usage: bb_core_cli --token T [--stale-ms N] [--fake-time] [--manual]\n");
    return 2;
  }

  FakeTimeSource fake_ts;
  fake_ts.t = 1000000;  // nonzero start
  SteadyTimeSource steady_ts;
  const TimeSource& ts = fake ? static_cast<const TimeSource&>(fake_ts)
                              : static_cast<const TimeSource&>(steady_ts);

  BoardConfig cfg;
  cfg.clock.stale_timeout_ms = stale_ms;
  cfg.manual_enabled = manual;
  Board board(ts, cfg);
  ControlHub hub(board, ts, token);

  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    JsonPtr req(cJSON_ParseWithLength(line.c_str(), line.size()));
    if (!req || !cJSON_IsObject(req.get())) {
      emit_error("malformed request");
      emit_done();
      continue;
    }
    const char* ch = str_of(req.get(), "ch");
    if (!ch) {
      emit_error("missing ch");
    } else if (std::strcmp(ch, "ws") == 0) {
      long long conn = 0;
      const char* ev = str_of(req.get(), "ev");
      if (!int_of(req.get(), "conn", conn) || !ev) {
        emit_error("ws requires conn and ev");
      } else if (std::strcmp(ev, "open") == 0) {
        hub.on_open(static_cast<ConnId>(conn));
      } else if (std::strcmp(ev, "close") == 0) {
        hub.on_close(static_cast<ConnId>(conn));
      } else if (std::strcmp(ev, "text") == 0) {
        const char* data = str_of(req.get(), "data");
        if (!data) {
          emit_error("ws text requires data");
        } else {
          std::vector<Outbound> outs;
          hub.on_text(static_cast<ConnId>(conn), data, std::strlen(data), outs);
          emit_outbound(outs);
        }
      } else {
        emit_error("unknown ws ev");
      }
    } else if (std::strcmp(ch, "http") == 0) {
      const cJSON* id = cJSON_GetObjectItemCaseSensitive(req.get(), "id");
      const char* path = str_of(req.get(), "path");
      if (!id || !path) {
        emit_error("http requires id and path");
      } else {
        const char* auth = str_of(req.get(), "auth");  // null/absent -> nullptr
        const char* body = str_of(req.get(), "body");
        if (!body) body = "";
        HttpReply rep;
        if (std::strcmp(path, "/state") == 0) {
          rep = hub.on_http_state(auth, body, std::strlen(body));
        } else if (std::strcmp(path, "/manual/cmd") == 0) {
          rep = hub.on_http_manual_cmd(auth, body, std::strlen(body));
        } else {
          rep.status = 404;
          rep.body = "{\"error\":\"not_found\"}";
        }
        cJSON* o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "ch", "http");
        cJSON_AddItemToObject(o, "id", cJSON_Duplicate(id, 1));
        cJSON_AddNumberToObject(o, "status", rep.status);
        cJSON_AddStringToObject(o, "body", rep.body.c_str());
        emit(o);
      }
    } else if (std::strcmp(ch, "tick") == 0) {
      std::vector<Outbound> outs;
      hub.tick(outs);
      emit_outbound(outs);
    } else if (std::strcmp(ch, "frame") == 0) {
      cJSON* o = cJSON_CreateObject();
      cJSON_AddStringToObject(o, "ch", "frame");
      cJSON_AddItemToObject(o, "frame", frame_json(board.frame()));
      emit(o);
    } else if (std::strcmp(ch, "status") == 0) {
      cJSON* o = cJSON_CreateObject();
      cJSON_AddStringToObject(o, "ch", "status");
      cJSON_AddStringToObject(o, "body", hub.status_json().c_str());
      emit(o);
    } else if (std::strcmp(ch, "time") == 0) {
      long long ms = 0;
      if (!fake) {
        emit_error("time requires --fake-time");
      } else if (!int_of(req.get(), "advance_ms", ms) || ms < 0) {
        emit_error("time requires non-negative advance_ms");
      } else {
        fake_ts.advance_ms(ms);
      }
    } else {
      emit_error(std::string("unknown ch: ") + ch);
    }
    emit_done();
  }
  return 0;
}
