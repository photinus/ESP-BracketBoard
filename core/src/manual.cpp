// ManualController: on-device clock/score. State is (base_, anchor_us_); current() is a pure
// function of those and TimeSource::now_us(). apply() first "settles" the running clock into a
// local copy, mutates the copy, and commits only if the whole command was valid.
//
// Documented behaviours:
//  - clock_start on an expired/zero clock returns false and changes nothing. clock_start while
//    already running is applied (idempotent). clock_start from injury resumes running.
//  - clock_injury is rejected (false) on an expired clock; otherwise state becomes injury.
//  - clock_stop on an expired clock leaves it expired (still returns true).
//  - clock_set / clock_adjust clamp to 0..3600000 ms. A result of 0 yields state Expired; a
//    positive result on an Expired clock yields Stopped. Other states are preserved.
//  - weight/bout: non-printable bytes are dropped, then truncated to 8 characters.
//  - numeric fields must be integral JSON numbers; non-integral values are malformed.
//  - reset keeps weight, bout and colors_swapped.
#include "bb/manual.h"
#include <cmath>
#include <cstring>
#include "cJSON.h"

namespace bb {

namespace {

constexpr int32_t kClockMax = 3600000;
constexpr int32_t kClockDefault = 120000;

int32_t clamp_i64(int64_t v, int32_t lo, int32_t hi) {
  return static_cast<int32_t>(v < lo ? lo : (v > hi ? hi : v));
}

bool get_int(const cJSON* obj, const char* key, int64_t& out) {
  const cJSON* it = cJSON_GetObjectItemCaseSensitive(obj, key);
  if (!cJSON_IsNumber(it)) return false;
  const double d = it->valuedouble;
  if (!std::isfinite(d) || d != std::floor(d)) return false;
  if (d > 1e12) out = static_cast<int64_t>(1e12);
  else if (d < -1e12) out = static_cast<int64_t>(-1e12);
  else out = static_cast<int64_t>(d);
  return true;
}

bool get_side(const cJSON* obj, bool& left) {
  const cJSON* it = cJSON_GetObjectItemCaseSensitive(obj, "side");
  if (!cJSON_IsString(it) || !it->valuestring) return false;
  if (std::strcmp(it->valuestring, "left") == 0) { left = true; return true; }
  if (std::strcmp(it->valuestring, "right") == 0) { left = false; return true; }
  return false;
}

bool get_label(const cJSON* obj, char out[kLabelMax + 1]) {
  const cJSON* it = cJSON_GetObjectItemCaseSensitive(obj, "value");
  if (!cJSON_IsString(it) || !it->valuestring) return false;
  size_t n = 0;
  for (const char* p = it->valuestring; *p && n < kLabelMax; ++p) {
    const unsigned char c = static_cast<unsigned char>(*p);
    if (c >= 0x20 && c <= 0x7E) out[n++] = static_cast<char>(c);
  }
  out[n] = '\0';
  return true;
}

// Applies a new remaining value, keeping state semantics described above.
void set_remaining(Snapshot& s, int32_t ms) {
  s.clock_ms_remaining = ms;
  if (ms == 0) s.clock_state = ClockState::Expired;
  else if (s.clock_state == ClockState::Expired) s.clock_state = ClockState::Stopped;
}

}  // namespace

ManualController::ManualController(const TimeSource& ts) : ts_(ts) {
  std::strcpy(base_.session, "manual");
  base_.clock_ms_remaining = kClockDefault;
  base_.clock_state = ClockState::Stopped;
  base_.screen = Screen::Score;
  base_.period = 1;
  anchor_us_ = ts_.now_us();
}

Snapshot ManualController::current() const {
  Snapshot s = base_;
  if (s.clock_state == ClockState::Running) {
    int64_t elapsed_ms = (ts_.now_us() - anchor_us_) / 1000;
    if (elapsed_ms < 0) elapsed_ms = 0;
    const int64_t rem = static_cast<int64_t>(s.clock_ms_remaining) - elapsed_ms;
    if (rem <= 0) {
      s.clock_ms_remaining = 0;
      s.clock_state = ClockState::Expired;
    } else {
      s.clock_ms_remaining = static_cast<int32_t>(rem);
    }
  }
  return s;
}

bool ManualController::apply(const char* json_cmd, size_t len) {
  if (!json_cmd || len == 0 || len > kMaxMessageBytes) return false;

  const char* end = nullptr;
  cJSON* root = cJSON_ParseWithLengthOpts(json_cmd, len, &end, 0);
  if (!root) return false;
  struct Guard { cJSON* p; ~Guard() { cJSON_Delete(p); } } guard{root};
  // Reject trailing garbage after the top-level value.
  for (const char* p = end; p && p < json_cmd + len; ++p) {
    if (*p != ' ' && *p != '\t' && *p != '\n' && *p != '\r' && *p != '\0') return false;
  }
  if (!cJSON_IsObject(root)) return false;
  const cJSON* cmd_it = cJSON_GetObjectItemCaseSensitive(root, "cmd");
  if (!cJSON_IsString(cmd_it) || !cmd_it->valuestring) return false;
  const char* cmd = cmd_it->valuestring;

  // Settle the running clock into a local copy (advance the anchor by whole ms only, so no
  // sub-millisecond time is lost).
  Snapshot s = base_;
  int64_t anchor = anchor_us_;
  const int64_t now = ts_.now_us();
  if (s.clock_state == ClockState::Running) {
    int64_t elapsed_ms = (now - anchor) / 1000;
    if (elapsed_ms < 0) elapsed_ms = 0;
    const int64_t rem = static_cast<int64_t>(s.clock_ms_remaining) - elapsed_ms;
    if (rem <= 0) {
      s.clock_ms_remaining = 0;
      s.clock_state = ClockState::Expired;
    } else {
      s.clock_ms_remaining = static_cast<int32_t>(rem);
    }
    anchor += elapsed_ms * 1000;
  }
  if (s.clock_state != ClockState::Running) anchor = now;

  const bool expired = s.clock_state == ClockState::Expired || s.clock_ms_remaining <= 0;

  if (std::strcmp(cmd, "clock_start") == 0) {
    if (expired) return false;
    if (s.clock_state != ClockState::Running) anchor = now;
    s.clock_state = ClockState::Running;
  } else if (std::strcmp(cmd, "clock_stop") == 0) {
    if (s.clock_state == ClockState::Running || s.clock_state == ClockState::Injury)
      s.clock_state = ClockState::Stopped;
  } else if (std::strcmp(cmd, "clock_injury") == 0) {
    if (expired) return false;
    s.clock_state = ClockState::Injury;
  } else if (std::strcmp(cmd, "clock_set") == 0) {
    int64_t ms;
    if (!get_int(root, "ms", ms)) return false;
    set_remaining(s, clamp_i64(ms, 0, kClockMax));
  } else if (std::strcmp(cmd, "clock_adjust") == 0) {
    int64_t d;
    if (!get_int(root, "delta_ms", d)) return false;
    set_remaining(s, clamp_i64(static_cast<int64_t>(s.clock_ms_remaining) + d, 0, kClockMax));
  } else if (std::strcmp(cmd, "score") == 0 || std::strcmp(cmd, "warning") == 0) {
    bool left;
    int64_t d;
    if (!get_side(root, left) || !get_int(root, "delta", d)) return false;
    const bool is_score = cmd[0] == 's';
    uint8_t& field = is_score ? (left ? s.left_score : s.right_score)
                              : (left ? s.left_warnings : s.right_warnings);
    field = static_cast<uint8_t>(clamp_i64(static_cast<int64_t>(field) + d, 0, is_score ? 99 : 9));
  } else if (std::strcmp(cmd, "period") == 0) {
    int64_t v;
    if (!get_int(root, "value", v)) return false;
    s.period = static_cast<uint8_t>(clamp_i64(v, 1, 9));
  } else if (std::strcmp(cmd, "weight") == 0) {
    if (!get_label(root, s.weight)) return false;
  } else if (std::strcmp(cmd, "bout") == 0) {
    if (!get_label(root, s.bout)) return false;
  } else if (std::strcmp(cmd, "swap") == 0) {
    s.colors_swapped = !s.colors_swapped;
  } else if (std::strcmp(cmd, "splash") == 0) {
    const cJSON* v = cJSON_GetObjectItemCaseSensitive(root, "value");
    if (!cJSON_IsBool(v)) return false;
    s.screen = cJSON_IsTrue(v) ? Screen::Splash : Screen::Score;
  } else if (std::strcmp(cmd, "reset") == 0) {
    s.left_score = s.right_score = 0;
    s.left_warnings = s.right_warnings = 0;
    s.period = 1;
    s.clock_ms_remaining = kClockDefault;
    s.clock_state = ClockState::Stopped;
    s.screen = Screen::Score;
  } else {
    return false;
  }

  if (s.clock_state != ClockState::Running) anchor = now;
  s.seq = base_.seq + 1;
  base_ = s;
  anchor_us_ = anchor;
  return true;
}

}  // namespace bb
