#include <cstring>
#include <string>
#include "bb/manual.h"
#include "bb/time_source.h"
#include "doctest.h"

using namespace bb;

namespace {
struct Fx {
  FakeTimeSource ts;
  ManualController m{ts};
  bool cmd(const std::string& j) { return m.apply(j.c_str(), j.size()); }
};
}  // namespace

TEST_CASE("manual: initial state") {
  Fx f;
  Snapshot s = f.m.current();
  CHECK(std::string(s.session) == "manual");
  CHECK(s.seq == 0);
  CHECK(s.left_score == 0);
  CHECK(s.right_score == 0);
  CHECK(s.left_warnings == 0);
  CHECK(s.right_warnings == 0);
  CHECK(s.period == 1);
  CHECK(s.clock_ms_remaining == 120000);
  CHECK(s.clock_state == ClockState::Stopped);
  CHECK(s.screen == Screen::Score);
  CHECK_FALSE(s.colors_swapped);
}

TEST_CASE("manual: score and warning with clamping") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"score","side":"left","delta":2})"));
  CHECK(f.cmd(R"({"cmd":"score","side":"right","delta":4})"));
  CHECK(f.m.current().left_score == 2);
  CHECK(f.m.current().right_score == 4);
  CHECK(f.cmd(R"({"cmd":"score","side":"left","delta":-5})"));
  CHECK(f.m.current().left_score == 0);
  CHECK(f.cmd(R"({"cmd":"score","side":"right","delta":500})"));
  CHECK(f.m.current().right_score == 99);
  CHECK(f.cmd(R"({"cmd":"warning","side":"left","delta":1})"));
  CHECK(f.m.current().left_warnings == 1);
  CHECK(f.cmd(R"({"cmd":"warning","side":"right","delta":20})"));
  CHECK(f.m.current().right_warnings == 9);
  CHECK(f.cmd(R"({"cmd":"warning","side":"right","delta":-20})"));
  CHECK(f.m.current().right_warnings == 0);
}

TEST_CASE("manual: period clamps 1..9") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"period","value":3})"));
  CHECK(f.m.current().period == 3);
  CHECK(f.cmd(R"({"cmd":"period","value":0})"));
  CHECK(f.m.current().period == 1);
  CHECK(f.cmd(R"({"cmd":"period","value":42})"));
  CHECK(f.m.current().period == 9);
}

TEST_CASE("manual: weight and bout") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"weight","value":"157"})"));
  CHECK(f.cmd(R"({"cmd":"bout","value":"1203"})"));
  CHECK(std::string(f.m.current().weight) == "157");
  CHECK(std::string(f.m.current().bout) == "1203");
  CHECK(f.cmd(R"({"cmd":"weight","value":"123456789012"})"));
  CHECK(std::string(f.m.current().weight) == "12345678");
  CHECK(f.cmd(R"({"cmd":"bout","value":"a\tbéc"})"));
  CHECK(std::string(f.m.current().bout) == "abc");  // non-printable dropped
  CHECK(f.cmd(R"({"cmd":"bout","value":""})"));
  CHECK(std::string(f.m.current().bout) == "");
  CHECK_FALSE(f.cmd(R"({"cmd":"weight","value":5})"));
}

TEST_CASE("manual: swap and splash") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"swap"})"));
  CHECK(f.m.current().colors_swapped);
  CHECK(f.cmd(R"({"cmd":"swap"})"));
  CHECK_FALSE(f.m.current().colors_swapped);
  CHECK(f.cmd(R"({"cmd":"splash","value":true})"));
  CHECK(f.m.current().screen == Screen::Splash);
  CHECK(f.cmd(R"({"cmd":"splash","value":false})"));
  CHECK(f.m.current().screen == Screen::Score);
  CHECK_FALSE(f.cmd(R"({"cmd":"splash","value":"yes"})"));
  CHECK_FALSE(f.cmd(R"({"cmd":"splash"})"));
}

TEST_CASE("manual: clock runs and expires itself at 0") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":5000})"));
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  CHECK(f.m.current().clock_state == ClockState::Running);
  CHECK(f.m.current().clock_ms_remaining == 5000);
  f.ts.advance_ms(1500);
  CHECK(f.m.current().clock_ms_remaining == 3500);
  CHECK(f.m.current().clock_state == ClockState::Running);
  // current() is pure: repeated calls give the same answer.
  CHECK(f.m.current().clock_ms_remaining == 3500);
  f.ts.advance_ms(3499);
  CHECK(f.m.current().clock_ms_remaining == 1);
  CHECK(f.m.current().clock_state == ClockState::Running);
  f.ts.advance_ms(1);
  CHECK(f.m.current().clock_ms_remaining == 0);
  CHECK(f.m.current().clock_state == ClockState::Expired);
  f.ts.advance_ms(10000);
  CHECK(f.m.current().clock_ms_remaining == 0);
  CHECK(f.m.current().clock_state == ClockState::Expired);
  // Starting an expired clock is rejected and changes nothing.
  uint32_t seq = f.m.current().seq;
  CHECK_FALSE(f.cmd(R"({"cmd":"clock_start"})"));
  CHECK(f.m.current().seq == seq);
  CHECK(f.m.current().clock_state == ClockState::Expired);
  // Stopping an expired clock leaves it expired.
  CHECK(f.cmd(R"({"cmd":"clock_stop"})"));
  CHECK(f.m.current().clock_state == ClockState::Expired);
  // Setting a new time re-arms it (Stopped), and it can then start.
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":2000})"));
  CHECK(f.m.current().clock_state == ClockState::Stopped);
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  f.ts.advance_ms(500);
  CHECK(f.m.current().clock_ms_remaining == 1500);
}

TEST_CASE("manual: stop and injury freeze the clock") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  f.ts.advance_ms(10000);
  CHECK(f.cmd(R"({"cmd":"clock_stop"})"));
  CHECK(f.m.current().clock_state == ClockState::Stopped);
  CHECK(f.m.current().clock_ms_remaining == 110000);
  f.ts.advance_ms(30000);
  CHECK(f.m.current().clock_ms_remaining == 110000);

  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  f.ts.advance_ms(10000);
  CHECK(f.cmd(R"({"cmd":"clock_injury"})"));
  CHECK(f.m.current().clock_state == ClockState::Injury);
  CHECK(f.m.current().clock_ms_remaining == 100000);
  f.ts.advance_ms(30000);
  CHECK(f.m.current().clock_ms_remaining == 100000);
  // Resume from injury.
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  CHECK(f.m.current().clock_state == ClockState::Running);
  f.ts.advance_ms(5000);
  CHECK(f.m.current().clock_ms_remaining == 95000);
  // Injury on an expired clock is rejected.
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":0})"));
  CHECK(f.m.current().clock_state == ClockState::Expired);
  CHECK_FALSE(f.cmd(R"({"cmd":"clock_injury"})"));
}

TEST_CASE("manual: no drift across many apply calls while running") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  for (int i = 0; i < 100; ++i) {
    f.ts.t += 1500;  // 1.5 ms steps in microseconds
    CHECK(f.cmd(R"({"cmd":"swap"})"));
  }
  // 150 ms elapsed in total.
  CHECK(f.m.current().clock_ms_remaining == 120000 - 150);
}

TEST_CASE("manual: clock_set and clock_adjust") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":90000})"));
  CHECK(f.m.current().clock_ms_remaining == 90000);
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":99999999})"));
  CHECK(f.m.current().clock_ms_remaining == 3600000);
  CHECK(f.cmd(R"({"cmd":"clock_set","ms":-5})"));
  CHECK(f.m.current().clock_ms_remaining == 0);
  CHECK(f.m.current().clock_state == ClockState::Expired);

  CHECK(f.cmd(R"({"cmd":"clock_set","ms":60000})"));
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":-1000})"));
  CHECK(f.m.current().clock_ms_remaining == 59000);
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":5000})"));
  CHECK(f.m.current().clock_ms_remaining == 64000);
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":-999999})"));
  CHECK(f.m.current().clock_ms_remaining == 0);
  CHECK(f.m.current().clock_state == ClockState::Expired);
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":5000})"));
  CHECK(f.m.current().clock_ms_remaining == 5000);
  CHECK(f.m.current().clock_state == ClockState::Stopped);
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":99999999})"));
  CHECK(f.m.current().clock_ms_remaining == 3600000);
}

TEST_CASE("manual: clock_adjust while running uses the live value") {
  Fx f;
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  f.ts.advance_ms(20000);
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":-5000})"));
  CHECK(f.m.current().clock_ms_remaining == 95000);
  CHECK(f.m.current().clock_state == ClockState::Running);
  f.ts.advance_ms(1000);
  CHECK(f.m.current().clock_ms_remaining == 94000);
  // Adjusting a running clock down to zero expires it.
  CHECK(f.cmd(R"({"cmd":"clock_adjust","delta_ms":-1000000})"));
  CHECK(f.m.current().clock_state == ClockState::Expired);
}

TEST_CASE("manual: reset") {
  Fx f;
  f.cmd(R"({"cmd":"score","side":"left","delta":7})");
  f.cmd(R"({"cmd":"score","side":"right","delta":3})");
  f.cmd(R"({"cmd":"warning","side":"left","delta":2})");
  f.cmd(R"({"cmd":"period","value":4})");
  f.cmd(R"({"cmd":"clock_set","ms":30000})");
  f.cmd(R"({"cmd":"clock_start"})");
  f.cmd(R"({"cmd":"splash","value":true})");
  f.ts.advance_ms(1000);
  CHECK(f.cmd(R"({"cmd":"reset"})"));
  Snapshot s = f.m.current();
  CHECK(s.left_score == 0);
  CHECK(s.right_score == 0);
  CHECK(s.left_warnings == 0);
  CHECK(s.right_warnings == 0);
  CHECK(s.period == 1);
  CHECK(s.clock_ms_remaining == 120000);
  CHECK(s.clock_state == ClockState::Stopped);
  CHECK(s.screen == Screen::Score);
  f.ts.advance_ms(5000);
  CHECK(f.m.current().clock_ms_remaining == 120000);
}

TEST_CASE("manual: malformed and unknown commands change nothing") {
  Fx f;
  f.cmd(R"({"cmd":"score","side":"left","delta":2})");
  const uint32_t seq = f.m.current().seq;
  const char* bad[] = {
      "",
      "not json",
      "{",
      "[]",
      "42",
      R"({})",
      R"({"cmd":5})",
      R"({"cmd":"nope"})",
      R"({"cmd":"CLOCK_START"})",
      R"({"cmd":"score","side":"up","delta":1})",
      R"({"cmd":"score","side":"left"})",
      R"({"cmd":"score","side":"left","delta":"1"})",
      R"({"cmd":"score","side":"left","delta":1.5})",
      R"({"cmd":"score","delta":1})",
      R"({"cmd":"warning","side":"left"})",
      R"({"cmd":"period"})",
      R"({"cmd":"period","value":"2"})",
      R"({"cmd":"clock_set"})",
      R"({"cmd":"clock_set","ms":"1000"})",
      R"({"cmd":"clock_adjust"})",
      R"({"cmd":"bout"})",
      R"({"cmd":"swap"} trailing)",
  };
  for (const char* b : bad) {
    INFO(b);
    CHECK_FALSE(f.m.apply(b, std::strlen(b)));
  }
  CHECK_FALSE(f.m.apply(nullptr, 0));
  CHECK_FALSE(f.m.apply(nullptr, 5));
  Snapshot s = f.m.current();
  CHECK(s.seq == seq);
  CHECK(s.left_score == 2);
  CHECK(s.right_score == 0);
  CHECK(s.clock_ms_remaining == 120000);
  CHECK_FALSE(s.colors_swapped);
}

TEST_CASE("manual: honours len and tolerates whitespace") {
  Fx f;
  const char* j = R"(  {"cmd":"swap"}  )";
  CHECK(f.m.apply(j, std::strlen(j)));
  // Only the first 5 bytes are visible: truncated JSON is malformed.
  CHECK_FALSE(f.m.apply(R"({"cmd":"swap"})", 5));
  CHECK(f.m.current().colors_swapped);
  // Over the protocol size limit.
  std::string big = R"({"cmd":"weight","value":")" + std::string(2000, 'a') + R"("})";
  CHECK_FALSE(f.m.apply(big.c_str(), big.size()));
}

TEST_CASE("manual: seq increments per applied command only") {
  Fx f;
  CHECK(f.m.current().seq == 0);
  CHECK(f.cmd(R"({"cmd":"swap"})"));
  CHECK(f.m.current().seq == 1);
  CHECK_FALSE(f.cmd(R"({"cmd":"bogus"})"));
  CHECK(f.m.current().seq == 1);
  CHECK(f.cmd(R"({"cmd":"clock_start"})"));
  CHECK(f.cmd(R"({"cmd":"clock_stop"})"));
  CHECK(f.m.current().seq == 3);
  f.ts.advance_ms(5000);  // time passing is not a command
  CHECK(f.m.current().seq == 3);
  CHECK(std::string(f.m.current().session) == "manual");
}
