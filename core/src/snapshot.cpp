// Snapshot parsing / validation / serialisation. See docs/protocol-v1.md section 1.
#include "bb/snapshot.h"

#include <cmath>
#include <cstdio>
#include <cstring>

#include "cJSON.h"

namespace bb {

namespace {

constexpr double kMaxSafeInt = 9007199254740991.0;  // 2^53 - 1

ParseResult fail(ParseError e, const char* field = nullptr) {
  ParseResult r;
  r.err = e;
  r.field = field;
  return r;
}

// Duplicate keys: last wins (cJSON_GetObjectItemCaseSensitive returns the first).
const cJSON* find_last(const cJSON* obj, const char* key) {
  const cJSON* found = nullptr;
  for (const cJSON* it = obj->child; it != nullptr; it = it->next) {
    if (it->string != nullptr && std::strcmp(it->string, key) == 0) found = it;
  }
  return found;
}

// Reads an integral number in [lo, hi]. Returns Ok/MissingField/BadType/OutOfRange.
ParseError get_int(const cJSON* obj, const char* key, double lo, double hi, double& val) {
  const cJSON* it = find_last(obj, key);
  if (it == nullptr) return ParseError::MissingField;
  if (!cJSON_IsNumber(it)) return ParseError::BadType;
  const double d = it->valuedouble;
  if (!std::isfinite(d) || std::floor(d) != d) return ParseError::BadType;
  if (d < lo || d > hi) return ParseError::OutOfRange;
  val = d;
  return ParseError::Ok;
}

ParseError get_string(const cJSON* obj, const char* key, const char*& s) {
  const cJSON* it = find_last(obj, key);
  if (it == nullptr) return ParseError::MissingField;
  if (!cJSON_IsString(it) || it->valuestring == nullptr) return ParseError::BadType;
  s = it->valuestring;
  return ParseError::Ok;
}

ParseError get_bool(const cJSON* obj, const char* key, bool& b) {
  const cJSON* it = find_last(obj, key);
  if (it == nullptr) return ParseError::MissingField;
  if (!cJSON_IsBool(it)) return ParseError::BadType;
  b = cJSON_IsTrue(it) != 0;
  return ParseError::Ok;
}

bool is_session_char(char c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' ||
         c == '-';
}

bool valid_session(const char* s) {
  const size_t n = std::strlen(s);
  if (n < 1 || n > kSessionMax) return false;
  for (size_t i = 0; i < n; ++i) {
    if (!is_session_char(s[i])) return false;
  }
  return true;
}

bool valid_label(const char* s) {
  const size_t n = std::strlen(s);
  if (n > kLabelMax) return false;
  for (size_t i = 0; i < n; ++i) {
    const unsigned char c = static_cast<unsigned char>(s[i]);
    if (c < 0x20 || c > 0x7E) return false;
  }
  return true;
}

// Appends to a bounded buffer; sets `ok` false on overflow.
struct Writer {
  char* buf;
  size_t cap;
  size_t len = 0;
  bool ok = true;

  void put(char c) {
    if (len + 1 >= cap) {
      ok = false;
      return;
    }
    buf[len++] = c;
  }
  void puts(const char* s) {
    while (*s) put(*s++);
  }
  void put_u64(unsigned long long v) {
    char tmp[24];
    std::snprintf(tmp, sizeof(tmp), "%llu", v);
    puts(tmp);
  }
  void put_i64(long long v) {
    char tmp[24];
    std::snprintf(tmp, sizeof(tmp), "%lld", v);
    puts(tmp);
  }
  void put_str(const char* s, size_t maxlen) {
    put('"');
    for (size_t i = 0; i < maxlen && s[i] != '\0'; ++i) {
      const unsigned char c = static_cast<unsigned char>(s[i]);
      if (c == '"' || c == '\\') {
        put('\\');
        put(static_cast<char>(c));
      } else if (c < 0x20 || c > 0x7E) {
        char tmp[8];
        std::snprintf(tmp, sizeof(tmp), "\\u%04x", static_cast<unsigned>(c));
        puts(tmp);
      } else {
        put(static_cast<char>(c));
      }
    }
    put('"');
  }
  void key(const char* k) {
    put('"');
    puts(k);
    puts("\":");
  }
};

}  // namespace

ParseResult parse_snapshot(const char* json, size_t len, Snapshot& out) {
  if (len > kMaxMessageBytes) return fail(ParseError::TooLarge);
  if (json == nullptr || len == 0) return fail(ParseError::NotJson);

  const char* end = nullptr;
  cJSON* root = cJSON_ParseWithLengthOpts(json, len, &end, 0);
  if (root == nullptr) return fail(ParseError::NotJson);

  // Reject trailing garbage after the top-level value.
  if (end != nullptr) {
    const char* limit = json + len;
    for (const char* p = end; p < limit && *p != '\0'; ++p) {
      if (*p != ' ' && *p != '\t' && *p != '\n' && *p != '\r') {
        cJSON_Delete(root);
        return fail(ParseError::NotJson);
      }
    }
  }

  ParseResult result;
  Snapshot s;  // built locally; copied to `out` only on success

  do {
    if (!cJSON_IsObject(root)) {
      result = fail(ParseError::NotObject);
      break;
    }

    // type
    const cJSON* type = find_last(root, "type");
    if (type == nullptr || !cJSON_IsString(type) || type->valuestring == nullptr ||
        std::strcmp(type->valuestring, "state") != 0) {
      result = fail(ParseError::WrongType, "type");
      break;
    }

    // v
    double d = 0;
    ParseError e = get_int(root, "v", -1e300, 1e300, d);
    if (e != ParseError::Ok) {
      result = fail(e, "v");
      break;
    }
    if (d != static_cast<double>(kProtocolVersion)) {
      result = fail(ParseError::UnsupportedVersion, "v");
      break;
    }
    s.v = static_cast<uint8_t>(kProtocolVersion);

    // session
    const char* str = nullptr;
    e = get_string(root, "session", str);
    if (e == ParseError::Ok && !valid_session(str)) e = ParseError::OutOfRange;
    if (e != ParseError::Ok) {
      result = fail(e, "session");
      break;
    }
    std::strcpy(s.session, str);

    // Integer fields in table order.
    struct IntField {
      const char* name;
      double lo, hi;
    };
    static const IntField kInts[] = {
        {"seq", 0, 4294967295.0},
        {"sender_ts", 0, kMaxSafeInt},
        {"left_score", 0, 99},
        {"right_score", 0, 99},
        {"left_warnings", 0, 9},
        {"right_warnings", 0, 9},
        {"period", 1, 9},
        {"clock_ms_remaining", 0, 3600000},
    };
    double vals[sizeof(kInts) / sizeof(kInts[0])] = {0};
    bool bad = false;
    for (size_t i = 0; i < sizeof(kInts) / sizeof(kInts[0]); ++i) {
      e = get_int(root, kInts[i].name, kInts[i].lo, kInts[i].hi, vals[i]);
      if (e != ParseError::Ok) {
        result = fail(e, kInts[i].name);
        bad = true;
        break;
      }
    }
    if (bad) break;
    s.seq = static_cast<uint32_t>(vals[0]);
    s.sender_ts = static_cast<int64_t>(vals[1]);
    s.left_score = static_cast<uint8_t>(vals[2]);
    s.right_score = static_cast<uint8_t>(vals[3]);
    s.left_warnings = static_cast<uint8_t>(vals[4]);
    s.right_warnings = static_cast<uint8_t>(vals[5]);
    s.period = static_cast<uint8_t>(vals[6]);
    s.clock_ms_remaining = static_cast<int32_t>(vals[7]);

    // clock_state
    e = get_string(root, "clock_state", str);
    if (e == ParseError::Ok) {
      if (std::strcmp(str, "running") == 0) s.clock_state = ClockState::Running;
      else if (std::strcmp(str, "stopped") == 0) s.clock_state = ClockState::Stopped;
      else if (std::strcmp(str, "injury") == 0) s.clock_state = ClockState::Injury;
      else if (std::strcmp(str, "expired") == 0) s.clock_state = ClockState::Expired;
      else e = ParseError::OutOfRange;
    }
    if (e != ParseError::Ok) {
      result = fail(e, "clock_state");
      break;
    }

    // screen
    e = get_string(root, "screen", str);
    if (e == ParseError::Ok) {
      if (std::strcmp(str, "score") == 0) s.screen = Screen::Score;
      else if (std::strcmp(str, "splash") == 0) s.screen = Screen::Splash;
      else e = ParseError::OutOfRange;
    }
    if (e != ParseError::Ok) {
      result = fail(e, "screen");
      break;
    }

    // weight
    e = get_string(root, "weight", str);
    if (e == ParseError::Ok && !valid_label(str)) e = ParseError::OutOfRange;
    if (e != ParseError::Ok) {
      result = fail(e, "weight");
      break;
    }
    std::strcpy(s.weight, str);

    // bout
    e = get_string(root, "bout", str);
    if (e == ParseError::Ok && !valid_label(str)) e = ParseError::OutOfRange;
    if (e != ParseError::Ok) {
      result = fail(e, "bout");
      break;
    }
    std::strcpy(s.bout, str);

    // colors_swapped
    e = get_bool(root, "colors_swapped", s.colors_swapped);
    if (e != ParseError::Ok) {
      result = fail(e, "colors_swapped");
      break;
    }
  } while (false);

  cJSON_Delete(root);
  if (result.ok()) out = s;
  return result;
}

const char* parse_error_code(ParseError e) {
  switch (e) {
    case ParseError::Ok: return "ok";
    case ParseError::TooLarge: return "too_large";
    case ParseError::NotJson: return "not_json";
    case ParseError::NotObject: return "not_object";
    case ParseError::WrongType: return "wrong_type";
    case ParseError::UnsupportedVersion: return "unsupported_version";
    case ParseError::MissingField: return "missing_field";
    case ParseError::BadType: return "bad_type";
    case ParseError::OutOfRange: return "out_of_range";
  }
  return "ok";
}

size_t snapshot_to_json(const Snapshot& s, char* buf, size_t cap) {
  if (buf == nullptr || cap == 0) return 0;
  Writer w{buf, cap};
  w.puts("{\"type\":\"state\",");
  w.key("v");
  w.put_u64(s.v);
  w.put(',');
  w.key("session");
  w.put_str(s.session, kSessionMax);
  w.put(',');
  w.key("seq");
  w.put_u64(s.seq);
  w.put(',');
  w.key("sender_ts");
  w.put_i64(static_cast<long long>(s.sender_ts));
  w.put(',');
  w.key("left_score");
  w.put_u64(s.left_score);
  w.put(',');
  w.key("right_score");
  w.put_u64(s.right_score);
  w.put(',');
  w.key("left_warnings");
  w.put_u64(s.left_warnings);
  w.put(',');
  w.key("right_warnings");
  w.put_u64(s.right_warnings);
  w.put(',');
  w.key("period");
  w.put_u64(s.period);
  w.put(',');
  w.key("clock_ms_remaining");
  w.put_i64(s.clock_ms_remaining);
  w.put(',');
  w.key("clock_state");
  w.put_str(clock_state_name(s.clock_state), 16);
  w.put(',');
  w.key("screen");
  w.put_str(screen_name(s.screen), 16);
  w.put(',');
  w.key("weight");
  w.put_str(s.weight, kLabelMax);
  w.put(',');
  w.key("bout");
  w.put_str(s.bout, kLabelMax);
  w.put(',');
  w.key("colors_swapped");
  w.puts(s.colors_swapped ? "true" : "false");
  w.put('}');
  if (!w.ok) return 0;
  buf[w.len] = '\0';
  return w.len;
}

}  // namespace bb
