#include <cstring>
#include <string>
#include "bb/board.h"
#include "bb/time_source.h"
#include "doctest.h"

using namespace bb;

namespace {

std::string state_json(const char* session, uint32_t seq, int clock_ms, const char* cs = "running",
                       int left = 3, int right = 1) {
  Snapshot s;
  std::strcpy(s.session, session);
  s.seq = seq;
  s.sender_ts = 1763000000123LL;
  s.left_score = static_cast<uint8_t>(left);
  s.right_score = static_cast<uint8_t>(right);
  s.left_warnings = 0;
  s.right_warnings = 1;
  s.period = 2;
  s.clock_ms_remaining = clock_ms;
  if (!std::strcmp(cs, "running")) s.clock_state = ClockState::Running;
  else if (!std::strcmp(cs, "stopped")) s.clock_state = ClockState::Stopped;
  else if (!std::strcmp(cs, "injury")) s.clock_state = ClockState::Injury;
  else s.clock_state = ClockState::Expired;
  std::strcpy(s.weight, "157");
  std::strcpy(s.bout, "1203");
  char buf[1024];
  size_t n = snapshot_to_json(s, buf, sizeof buf);
  REQUIRE(n > 0);
  return std::string(buf, n);
}

struct Fx {
  FakeTimeSource ts;
  Board b;
  explicit Fx(bool manual = false) : ts(), b(ts, make(manual)) { ts.t = 1000000; }
  static BoardConfig make(bool manual) {
    BoardConfig c;
    c.manual_enabled = manual;
    return c;
  }
  IngestResult send(const std::string& j, Source s = Source::Ws) {
    return b.ingest_state(j.c_str(), j.size(), s);
  }
};

}  // namespace

TEST_CASE("board: defaults") {
  Fx f;
  CHECK_FALSE(f.b.manual_enabled());
  CHECK_FALSE(f.b.manual_active());
  CHECK_FALSE(f.b.agent_live());
  CHECK_FALSE(f.b.has_agent_data());
  Frame fr = f.b.frame();
  CHECK(fr.waiting);
  CHECK_FALSE(fr.manual);
  CHECK(std::string(fr.clock) == "-:--");
  CHECK_FALSE(fr.colon_on);
}

TEST_CASE("board: agent snapshot populates frame") {
  Fx f;
  auto r = f.send(state_json("abc", 1, 87340));
  CHECK(r.kind == IngestResult::Applied);
  CHECK(r.seq == 1);
  CHECK(f.b.agent_live());
  CHECK(f.b.has_agent_data());
  Frame fr = f.b.frame();
  CHECK_FALSE(fr.waiting);
  CHECK_FALSE(fr.manual);
  CHECK_FALSE(fr.stale);
  CHECK(fr.colon_on);
  CHECK(fr.screen == Screen::Score);
  CHECK(std::string(fr.clock) == "1:28");
  CHECK(fr.left_score == 3);
  CHECK(fr.right_score == 1);
  CHECK(fr.left_warnings == 0);
  CHECK(fr.right_warnings == 1);
  CHECK(fr.period == 2);
  CHECK(fr.clock_state == ClockState::Running);
  CHECK(std::string(fr.weight) == "157");
  CHECK(std::string(fr.bout) == "1203");
  CHECK_FALSE(fr.colors_swapped);
}

TEST_CASE("board: clock interpolates between snapshots") {
  Fx f;
  f.send(state_json("abc", 1, 60000));
  f.ts.advance_ms(1000);
  CHECK(std::string(f.b.frame().clock) == "0:59");
  f.ts.advance_ms(1000);
  CHECK(std::string(f.b.frame().clock) == "0:58");
  CHECK(f.b.agent_live());
}

TEST_CASE("board: stale after 3s freezes clock and colon") {
  Fx f;
  f.send(state_json("abc", 1, 60000));
  f.ts.advance_ms(3000);
  CHECK(f.b.agent_live());  // exactly at the timeout: not yet stale
  CHECK_FALSE(f.b.frame().stale);
  f.ts.advance_ms(1);
  CHECK_FALSE(f.b.agent_live());
  Frame fr = f.b.frame();
  CHECK(fr.stale);
  CHECK_FALSE(fr.colon_on);
  CHECK(std::string(fr.clock) == "0:57");
  CHECK(fr.left_score == 3);
  CHECK(fr.clock_state == ClockState::Running);
  f.ts.advance_ms(10000);
  CHECK(std::string(f.b.frame().clock) == "0:57");  // frozen
}

TEST_CASE("board: duplicate/stale/invalid do not refresh liveness") {
  Fx f;
  const std::string a = state_json("abc", 5, 60000);
  CHECK(f.send(a).kind == IngestResult::Applied);
  f.ts.advance_ms(2000);
  CHECK(f.send(a).kind == IngestResult::Duplicate);
  CHECK(f.send(state_json("abc", 4, 59000)).kind == IngestResult::Stale);
  auto inv = f.send("{\"type\":\"state\"}");
  CHECK(inv.kind == IngestResult::Invalid);
  CHECK(inv.parse.err == ParseError::MissingField);
  f.ts.advance_ms(1001);  // 3001 ms since the accepted one
  CHECK_FALSE(f.b.agent_live());
  Frame fr = f.b.frame();
  CHECK(fr.stale);
  CHECK(std::string(fr.clock) == "0:57");  // clock untouched by the rejected snapshots
  CHECK(fr.left_score == 3);
}

TEST_CASE("board: rejected snapshot does not change displayed fields") {
  Fx f;
  f.send(state_json("abc", 5, 60000, "running", 3, 1));
  f.send(state_json("abc", 4, 60000, "running", 9, 9));
  Frame fr = f.b.frame();
  CHECK(fr.left_score == 3);
  CHECK(fr.right_score == 1);
}

TEST_CASE("board: manual disabled never shows manual frame") {
  Fx f;
  const std::string c = "{\"cmd\":\"clock_set\",\"ms\":90000}";
  CHECK(f.b.manual().apply(c.c_str(), c.size()));
  CHECK_FALSE(f.b.manual_active());
  CHECK(f.b.frame().waiting);
}

TEST_CASE("board: manual enabled with no agent shows manual frame") {
  Fx f(true);
  CHECK(f.b.manual_enabled());
  CHECK(f.b.manual_active());
  const std::string c1 = "{\"cmd\":\"score\",\"side\":\"left\",\"delta\":2}";
  CHECK(f.b.manual().apply(c1.c_str(), c1.size()));
  Frame fr = f.b.frame();
  CHECK(fr.manual);
  CHECK_FALSE(fr.waiting);
  CHECK(fr.colon_on);
  CHECK_FALSE(fr.stale);
  CHECK(fr.left_score == 2);
  CHECK(std::string(fr.clock) == "2:00");
  CHECK(fr.clock_state == ClockState::Stopped);
}

TEST_CASE("board: manual clock runs on the frame") {
  Fx f(true);
  const std::string c = "{\"cmd\":\"clock_start\"}";
  CHECK(f.b.manual().apply(c.c_str(), c.size()));
  f.ts.advance_ms(5000);
  Frame fr = f.b.frame();
  CHECK(fr.manual);
  CHECK(std::string(fr.clock) == "1:55");
  CHECK(fr.clock_state == ClockState::Running);
}

TEST_CASE("board: agent snapshot takes over manual immediately, manual returns when stale") {
  Fx f(true);
  const std::string c = "{\"cmd\":\"score\",\"side\":\"right\",\"delta\":4}";
  f.b.manual().apply(c.c_str(), c.size());
  CHECK(f.b.frame().manual);

  CHECK(f.send(state_json("abc", 1, 60000)).kind == IngestResult::Applied);
  CHECK_FALSE(f.b.manual_active());
  Frame fr = f.b.frame();
  CHECK_FALSE(fr.manual);
  CHECK(fr.left_score == 3);
  CHECK(fr.right_score == 1);

  f.ts.advance_ms(3000);
  CHECK_FALSE(f.b.manual_active());
  f.ts.advance_ms(1);
  CHECK(f.b.manual_active());
  fr = f.b.frame();
  CHECK(fr.manual);
  CHECK(fr.right_score == 4);  // manual state was kept, not reset

  // agent returns
  CHECK(f.send(state_json("abc", 2, 50000)).kind == IngestResult::Applied);
  CHECK_FALSE(f.b.frame().manual);
}

TEST_CASE("board: WS and HTTP share one seq filter") {
  Fx f;
  const std::string a = state_json("abc", 7, 60000);
  CHECK(f.send(a, Source::Ws).kind == IngestResult::Applied);
  CHECK(f.send(a, Source::Http).kind == IngestResult::Duplicate);
  CHECK(f.send(state_json("abc", 6, 60000), Source::Http).kind == IngestResult::Stale);
  CHECK(f.send(state_json("abc", 8, 60000), Source::Http).kind == IngestResult::Applied);
  CHECK(f.send(state_json("abc", 8, 60000), Source::Ws).kind == IngestResult::Duplicate);
}

TEST_CASE("board: agent restart with new session is applied") {
  Fx f;
  f.send(state_json("old", 500, 60000));
  CHECK(f.send(state_json("new", 1, 30000, "stopped", 0, 0)).kind == IngestResult::Applied);
  Frame fr = f.b.frame();
  CHECK(fr.left_score == 0);
  CHECK(std::string(fr.clock) == "0:30");
  CHECK(std::string(f.b.seq().session()) == "new");
}

TEST_CASE("board: custom stale timeout from config") {
  FakeTimeSource ts;
  ts.t = 1000000;
  BoardConfig c;
  c.clock.stale_timeout_ms = 5000;
  Board b(ts, c);
  std::string j = state_json("abc", 1, 60000);
  b.ingest_state(j.c_str(), j.size(), Source::Ws);
  ts.advance_ms(4999);
  CHECK(b.agent_live());
  ts.advance_ms(2);
  CHECK_FALSE(b.agent_live());
}

TEST_CASE("board: splash screen and colors_swapped pass through") {
  Fx f;
  Snapshot s;
  std::strcpy(s.session, "abc");
  s.seq = 1;
  s.screen = Screen::Splash;
  s.colors_swapped = true;
  s.clock_state = ClockState::Expired;
  s.clock_ms_remaining = 0;
  char buf[1024];
  size_t n = snapshot_to_json(s, buf, sizeof buf);
  f.b.ingest_state(buf, n, Source::Ws);
  Frame fr = f.b.frame();
  CHECK(fr.screen == Screen::Splash);
  CHECK(fr.colors_swapped);
  CHECK(fr.clock_state == ClockState::Expired);
  CHECK(std::string(fr.clock) == "0:00");
}
