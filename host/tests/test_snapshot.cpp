#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include "bb/snapshot.h"
#include "doctest.h"

namespace fs = std::filesystem;
using namespace bb;

namespace {

std::string read_file(const fs::path& p) {
  std::ifstream f(p, std::ios::binary);
  std::stringstream ss;
  ss << f.rdbuf();
  return ss.str();
}

std::vector<fs::path> fixtures(const char* sub) {
  std::vector<fs::path> v;
  const fs::path dir = fs::path(BB_FIXTURE_DIR) / sub;
  for (const auto& e : fs::directory_iterator(dir)) {
    if (e.is_regular_file() && e.path().extension() == ".json") v.push_back(e.path());
  }
  return v;
}

using Fields = std::vector<std::pair<std::string, std::string>>;

Fields base_fields() {
  return {{"type", "\"state\""},
          {"v", "1"},
          {"session", "\"a1b2c3d4e5f6\""},
          {"seq", "1042"},
          {"sender_ts", "1763000000123"},
          {"left_score", "3"},
          {"right_score", "1"},
          {"left_warnings", "0"},
          {"right_warnings", "1"},
          {"period", "2"},
          {"clock_ms_remaining", "87340"},
          {"clock_state", "\"running\""},
          {"screen", "\"score\""},
          {"weight", "\"157\""},
          {"bout", "\"1203\""},
          {"colors_swapped", "false"}};
}

std::string build(const Fields& f) {
  std::string s = "{";
  for (size_t i = 0; i < f.size(); ++i) {
    if (i) s += ",";
    s += "\"" + f[i].first + "\":" + f[i].second;
  }
  return s + "}";
}

// Base message with `key` set to raw JSON `val`.
std::string with(const std::string& key, const std::string& val) {
  Fields f = base_fields();
  for (auto& kv : f) {
    if (kv.first == key) kv.second = val;
  }
  return build(f);
}

std::string without(const std::string& key) {
  Fields f = base_fields();
  for (size_t i = 0; i < f.size(); ++i) {
    if (f[i].first == key) {
      f.erase(f.begin() + static_cast<long>(i));
      break;
    }
  }
  return build(f);
}

ParseResult parse(const std::string& s, Snapshot& out) {
  return parse_snapshot(s.data(), s.size(), out);
}

bool same(const Snapshot& a, const Snapshot& b) {
  return a.v == b.v && std::strcmp(a.session, b.session) == 0 && a.seq == b.seq &&
         a.sender_ts == b.sender_ts && a.left_score == b.left_score &&
         a.right_score == b.right_score && a.left_warnings == b.left_warnings &&
         a.right_warnings == b.right_warnings && a.period == b.period &&
         a.clock_ms_remaining == b.clock_ms_remaining && a.clock_state == b.clock_state &&
         a.screen == b.screen && std::strcmp(a.weight, b.weight) == 0 &&
         std::strcmp(a.bout, b.bout) == 0 && a.colors_swapped == b.colors_swapped;
}

Snapshot sentinel() {
  Snapshot s;
  s.v = 1;
  std::strcpy(s.session, "SENTINEL");
  s.seq = 777;
  s.sender_ts = 888;
  s.left_score = 11;
  s.right_score = 22;
  s.left_warnings = 4;
  s.right_warnings = 5;
  s.period = 7;
  s.clock_ms_remaining = 12345;
  s.clock_state = ClockState::Injury;
  s.screen = Screen::Splash;
  std::strcpy(s.weight, "WW");
  std::strcpy(s.bout, "BB");
  s.colors_swapped = true;
  return s;
}

void expect_err(const std::string& msg, ParseError e, const char* field = nullptr) {
  Snapshot out = sentinel();
  const Snapshot before = out;
  const ParseResult r = parse(msg, out);
  CAPTURE(msg);
  CHECK(r.err == e);
  CHECK_FALSE(r.ok());
  if (field) {
    REQUIRE(r.field != nullptr);
    CHECK(std::string(r.field) == field);
  }
  CHECK(same(out, before));
}

void expect_ok(const std::string& msg, Snapshot& out) {
  const ParseResult r = parse(msg, out);
  CAPTURE(msg);
  REQUIRE(r.ok());
  CHECK(r.field == nullptr);
}

}  // namespace

TEST_CASE("parse_error_code strings") {
  CHECK(std::string(parse_error_code(ParseError::Ok)) == "ok");
  CHECK(std::string(parse_error_code(ParseError::TooLarge)) == "too_large");
  CHECK(std::string(parse_error_code(ParseError::NotJson)) == "not_json");
  CHECK(std::string(parse_error_code(ParseError::NotObject)) == "not_object");
  CHECK(std::string(parse_error_code(ParseError::WrongType)) == "wrong_type");
  CHECK(std::string(parse_error_code(ParseError::UnsupportedVersion)) == "unsupported_version");
  CHECK(std::string(parse_error_code(ParseError::MissingField)) == "missing_field");
  CHECK(std::string(parse_error_code(ParseError::BadType)) == "bad_type");
  CHECK(std::string(parse_error_code(ParseError::OutOfRange)) == "out_of_range");
}

TEST_CASE("valid fixtures parse") {
  const auto files = fixtures("valid");
  REQUIRE(files.size() >= 7);
  for (const auto& p : files) {
    CAPTURE(p.filename().string());
    const std::string s = read_file(p);
    Snapshot out;
    const ParseResult r = parse(s, out);
    CHECK(r.ok());
  }
}

TEST_CASE("invalid fixtures fail with the code named by the filename") {
  const auto files = fixtures("invalid");
  REQUIRE(files.size() >= 50);
  for (const auto& p : files) {
    const std::string name = p.filename().string();
    CAPTURE(name);
    const size_t pos = name.find("__");
    REQUIRE(pos != std::string::npos);
    const std::string want = name.substr(0, pos);
    const std::string s = read_file(p);
    Snapshot out = sentinel();
    const Snapshot before = out;
    const ParseResult r = parse(s, out);
    CHECK(std::string(parse_error_code(r.err)) == want);
    CHECK(same(out, before));
  }
}

TEST_CASE("valid fixture values") {
  Snapshot s;
  expect_ok(read_file(fs::path(BB_FIXTURE_DIR) / "valid" / "typical.json"), s);
  CHECK(s.v == 1);
  CHECK(std::string(s.session) == "a1b2c3d4e5f6");
  CHECK(s.seq == 1042);
  CHECK(s.sender_ts == 1763000000123LL);
  CHECK(s.left_score == 3);
  CHECK(s.right_score == 1);
  CHECK(s.left_warnings == 0);
  CHECK(s.right_warnings == 1);
  CHECK(s.period == 2);
  CHECK(s.clock_ms_remaining == 87340);
  CHECK(s.clock_state == ClockState::Running);
  CHECK(s.screen == Screen::Score);
  CHECK(std::string(s.weight) == "157");
  CHECK(std::string(s.bout) == "1203");
  CHECK_FALSE(s.colors_swapped);

  expect_ok(read_file(fs::path(BB_FIXTURE_DIR) / "valid" / "all_max.json"), s);
  CHECK(s.seq == 4294967295u);
  CHECK(s.sender_ts == 9007199254740991LL);
  CHECK(s.left_score == 99);
  CHECK(s.clock_ms_remaining == 3600000);
  CHECK(std::string(s.session) == "abcdefghijklmnop");
  CHECK(std::string(s.weight) == "12345678");
  CHECK(s.colors_swapped);
}

TEST_CASE("unknown fields are ignored") {
  Fields f = base_fields();
  f.push_back({"left_name", "\"Smith\""});
  f.push_back({"injury_ms", "12000"});
  f.push_back({"aux", "{\"a\":[1,2,{\"b\":null}]}"});
  f.push_back({"zzz", "null"});
  Snapshot s;
  expect_ok(build(f), s);
  CHECK(s.seq == 1042);
  CHECK(s.left_score == 3);
}

TEST_CASE("duplicate keys: last wins") {
  Fields f = base_fields();
  f.push_back({"left_score", "42"});
  Snapshot s;
  expect_ok(build(f), s);
  CHECK(s.left_score == 42);

  // Last one invalid -> rejected even though the first was valid.
  Fields g = base_fields();
  g.push_back({"left_score", "100"});
  expect_err(build(g), ParseError::OutOfRange, "left_score");
}

TEST_CASE("structural errors and precedence") {
  expect_err("", ParseError::NotJson);
  expect_err("{", ParseError::NotJson);
  expect_err(with("seq", "1") + " x", ParseError::NotJson);  // trailing garbage
  expect_err("[]", ParseError::NotObject);
  expect_err("\"state\"", ParseError::NotObject);
  expect_err("42", ParseError::NotObject);
  expect_err("null", ParseError::NotObject);

  // Trailing whitespace is fine.
  Snapshot s;
  expect_ok(build(base_fields()) + " \r\n\t ", s);

  expect_err(without("type"), ParseError::WrongType, "type");
  expect_err(with("type", "\"hello\""), ParseError::WrongType, "type");
  expect_err(with("type", "5"), ParseError::WrongType, "type");

  expect_err(with("v", "2"), ParseError::UnsupportedVersion, "v");
  expect_err(with("v", "0"), ParseError::UnsupportedVersion, "v");
  expect_err(with("v", "-1"), ParseError::UnsupportedVersion, "v");
  expect_err(without("v"), ParseError::MissingField, "v");
  expect_err(with("v", "\"1\""), ParseError::BadType, "v");
  expect_err(with("v", "1.5"), ParseError::BadType, "v");

  // wrong_type beats unsupported_version beats per-field errors.
  {
    Fields f = base_fields();
    f[0].second = "\"hello\"";
    f[1].second = "2";
    f[5].second = "1000";
    expect_err(build(f), ParseError::WrongType, "type");
    f[0].second = "\"state\"";
    expect_err(build(f), ParseError::UnsupportedVersion, "v");
    f[1].second = "1";
    expect_err(build(f), ParseError::OutOfRange, "left_score");
  }

  // Per-field errors in table order: seq (bad_type) is reported before left_score (out of range).
  {
    Fields f = base_fields();
    f[3].second = "\"x\"";
    f[5].second = "1000";
    expect_err(build(f), ParseError::BadType, "seq");
  }
  // Missing earlier field wins over a later invalid one.
  {
    Fields f = base_fields();
    f.erase(f.begin() + 2);  // session
    f[4].second = "1000";    // left_score
    expect_err(build(f), ParseError::MissingField, "session");
  }
}

TEST_CASE("too_large is checked before parsing") {
  std::string big(1025, ' ');
  expect_err(big, ParseError::TooLarge);
  // Valid JSON padded to 1025 bytes.
  std::string pad = build(base_fields());
  pad.append(1025 - pad.size(), ' ');
  REQUIRE(pad.size() == 1025);
  expect_err(pad, ParseError::TooLarge);
  // Exactly 1024 bytes of valid JSON is accepted.
  pad.pop_back();
  Snapshot s;
  expect_ok(pad, s);
  // Garbage over the limit is too_large, not not_json.
  expect_err(std::string(5000, '{'), ParseError::TooLarge);
}

TEST_CASE("missing fields report the field name") {
  for (const auto& kv : base_fields()) {
    if (kv.first == "type" || kv.first == "v") continue;
    CAPTURE(kv.first);
    expect_err(without(kv.first), ParseError::MissingField, kv.first.c_str());
  }
}

TEST_CASE("bad types report the field name") {
  struct C { const char* key; const char* val; };
  const C cases[] = {
      {"session", "5"},          {"seq", "\"1\""},          {"seq", "1.5"},
      {"sender_ts", "true"},     {"left_score", "null"},    {"right_score", "[1]"},
      {"left_warnings", "{}"},   {"right_warnings", "\"0\""}, {"period", "2.0001"},
      {"clock_ms_remaining", "\"1\""}, {"clock_state", "1"}, {"screen", "false"},
      {"weight", "157"},         {"bout", "null"},          {"colors_swapped", "0"},
      {"colors_swapped", "\"true\""},
  };
  for (const auto& c : cases) {
    CAPTURE(c.key);
    CAPTURE(c.val);
    expect_err(with(c.key, c.val), ParseError::BadType, c.key);
  }
}

TEST_CASE("out of range values report the field name") {
  struct C { const char* key; const char* val; };
  const C cases[] = {
      {"session", "\"\""},
      {"session", "\"12345678901234567\""},
      {"session", "\"bad session\""},
      {"session", "\"a.b\""},
      {"seq", "-1"},
      {"seq", "4294967296"},
      {"sender_ts", "-1"},
      {"sender_ts", "9007199254740992"},
      {"left_score", "100"},
      {"left_score", "-1"},
      {"right_score", "100"},
      {"left_warnings", "10"},
      {"right_warnings", "10"},
      {"period", "0"},
      {"period", "10"},
      {"clock_ms_remaining", "-1"},
      {"clock_ms_remaining", "3600001"},
      {"clock_state", "\"paused\""},
      {"clock_state", "\"Running\""},
      {"screen", "\"menu\""},
      {"weight", "\"123456789\""},
      {"bout", "\"123456789\""},
      {"weight", "\"tab\\there\""},
      {"bout", "\"caf\\u00e9\""},
      {"weight", "\"\\u007f\""},
      {"left_score", "1e3"},
  };
  for (const auto& c : cases) {
    CAPTURE(c.key);
    CAPTURE(c.val);
    expect_err(with(c.key, c.val), ParseError::OutOfRange, c.key);
  }
}

TEST_CASE("boundary values") {
  struct C { const char* key; const char* lo; const char* hi; };
  const C cases[] = {
      {"seq", "0", "4294967295"},
      {"sender_ts", "0", "9007199254740991"},
      {"left_score", "0", "99"},
      {"right_score", "0", "99"},
      {"left_warnings", "0", "9"},
      {"right_warnings", "0", "9"},
      {"period", "1", "9"},
      {"clock_ms_remaining", "0", "3600000"},
  };
  for (const auto& c : cases) {
    CAPTURE(c.key);
    Snapshot s;
    expect_ok(with(c.key, c.lo), s);
    expect_ok(with(c.key, c.hi), s);
  }
  Snapshot s;
  expect_ok(with("left_score", "99"), s);
  CHECK(s.left_score == 99);
  expect_err(with("left_score", "100"), ParseError::OutOfRange, "left_score");

  // Integral floats are accepted.
  expect_ok(with("left_score", "5.0"), s);
  CHECK(s.left_score == 5);
  expect_ok(with("left_score", "1e1"), s);
  CHECK(s.left_score == 10);

  // Strings: empty labels, 8 bytes, session of 1 and 16 chars, charset.
  expect_ok(with("weight", "\"\""), s);
  CHECK(std::string(s.weight).empty());
  expect_ok(with("bout", "\"\""), s);
  expect_ok(with("weight", "\"12345678\""), s);
  CHECK(std::string(s.weight) == "12345678");
  expect_ok(with("weight", "\" ~ \""), s);  // 0x20 and 0x7E
  CHECK(std::string(s.weight) == " ~ ");
  expect_ok(with("weight", "\"a\\\"b\\\\c\""), s);
  CHECK(std::string(s.weight) == "a\"b\\c");
  expect_ok(with("session", "\"a\""), s);
  expect_ok(with("session", "\"AZaz09_-AZaz09_-\""), s);
  CHECK(std::string(s.session) == "AZaz09_-AZaz09_-");
}

TEST_CASE("enum values") {
  Snapshot s;
  expect_ok(with("clock_state", "\"running\""), s);
  CHECK(s.clock_state == ClockState::Running);
  expect_ok(with("clock_state", "\"stopped\""), s);
  CHECK(s.clock_state == ClockState::Stopped);
  expect_ok(with("clock_state", "\"injury\""), s);
  CHECK(s.clock_state == ClockState::Injury);
  expect_ok(with("clock_state", "\"expired\""), s);
  CHECK(s.clock_state == ClockState::Expired);
  expect_ok(with("screen", "\"score\""), s);
  CHECK(s.screen == Screen::Score);
  expect_ok(with("screen", "\"splash\""), s);
  CHECK(s.screen == Screen::Splash);
  expect_ok(with("colors_swapped", "true"), s);
  CHECK(s.colors_swapped);
}

TEST_CASE("snapshot_to_json round-trips all valid fixtures") {
  for (const auto& p : fixtures("valid")) {
    CAPTURE(p.filename().string());
    Snapshot a;
    REQUIRE(parse(read_file(p), a).ok());
    char buf[kMaxMessageBytes];
    const size_t n = snapshot_to_json(a, buf, sizeof(buf));
    REQUIRE(n > 0);
    CHECK(n == std::strlen(buf));
    CHECK(n <= kMaxMessageBytes);
    Snapshot b = sentinel();
    const ParseResult r = parse_snapshot(buf, n, b);
    CHECK(r.ok());
    CHECK(same(a, b));
  }
}

TEST_CASE("snapshot_to_json output format and escaping") {
  Snapshot s;
  expect_ok(read_file(fs::path(BB_FIXTURE_DIR) / "valid" / "typical.json"), s);
  char buf[512];
  const size_t n = snapshot_to_json(s, buf, sizeof(buf));
  CHECK(std::string(buf, n) ==
        "{\"type\":\"state\",\"v\":1,\"session\":\"a1b2c3d4e5f6\",\"seq\":1042,"
        "\"sender_ts\":1763000000123,\"left_score\":3,\"right_score\":1,\"left_warnings\":0,"
        "\"right_warnings\":1,\"period\":2,\"clock_ms_remaining\":87340,"
        "\"clock_state\":\"running\",\"screen\":\"score\",\"weight\":\"157\",\"bout\":\"1203\","
        "\"colors_swapped\":false}");

  // Quote and backslash in labels survive a round trip.
  Snapshot q;
  expect_ok(with("weight", "\"a\\\"b\\\\c\\\"\\\\\""), q);  // a"b\c"\  (7 bytes)
  q.screen = Screen::Splash;
  q.clock_state = ClockState::Expired;
  q.colors_swapped = true;
  const size_t m = snapshot_to_json(q, buf, sizeof(buf));
  REQUIRE(m > 0);
  Snapshot r;
  REQUIRE(parse_snapshot(buf, m, r).ok());
  CHECK(same(q, r));
}

TEST_CASE("snapshot_to_json returns 0 when the buffer is too small") {
  Snapshot s;
  expect_ok(read_file(fs::path(BB_FIXTURE_DIR) / "valid" / "all_max.json"), s);
  char big[1024];
  const size_t n = snapshot_to_json(s, big, sizeof(big));
  REQUIRE(n > 0);

  char small[1024];
  CHECK(snapshot_to_json(s, small, 0) == 0);
  CHECK(snapshot_to_json(s, small, 1) == 0);
  CHECK(snapshot_to_json(s, small, 16) == 0);
  CHECK(snapshot_to_json(s, small, n) == 0);  // no room for the NUL
  CHECK(snapshot_to_json(s, small, n - 1) == 0);
  CHECK(snapshot_to_json(s, small, n + 1) == n);  // exact fit including NUL
  CHECK(std::strcmp(small, big) == 0);
  CHECK(snapshot_to_json(s, nullptr, 100) == 0);
}
