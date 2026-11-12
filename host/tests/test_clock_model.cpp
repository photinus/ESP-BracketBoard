#include <string>

#include "doctest.h"
#include "bb/clock_model.h"

using namespace bb;

TEST_CASE("clock: no data") {
  FakeTimeSource ts;
  ClockModel c(ts);
  CHECK_FALSE(c.has_data());
  CHECK_FALSE(c.stale());
  ClockView v = c.sample();
  CHECK(v.display_ms == 0);
  CHECK_FALSE(v.colon_on);
  CHECK_FALSE(v.stale);
  ts.advance_ms(60000);
  CHECK(c.sample().display_ms == 0);
  CHECK_FALSE(c.sample().colon_on);
}

TEST_CASE("clock: running counts down") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  CHECK(c.has_data());
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == 0);
  CHECK(c.sample().display_ms == 60000);
  CHECK(c.sample().colon_on);
  ts.advance_ms(1500);
  ClockView v = c.sample();
  CHECK(v.display_ms == 58500);
  CHECK(v.state == ClockState::Running);
  CHECK_FALSE(v.stale);
  CHECK(v.colon_on);
}

TEST_CASE("clock: stopped and injury never move; expired is 0") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(45000, ClockState::Stopped);
  ts.advance_ms(2500);
  CHECK(c.sample().display_ms == 45000);
  ts.advance_ms(100);
  c.on_snapshot(45000, ClockState::Injury);
  ts.advance_ms(2000);
  ClockView v = c.sample();
  CHECK(v.display_ms == 45000);
  CHECK(v.state == ClockState::Injury);
  ts.advance_ms(100);
  c.on_snapshot(12345, ClockState::Expired);
  CHECK(c.sample().display_ms == 0);
  CHECK(c.sample().state == ClockState::Expired);
}

TEST_CASE("clock: 200 ms drift slews at 100 ms/s") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  CHECK(c.sample().display_ms == 59000);
  // Agent says 58800 (we are 200 ms ahead on the clock => display too high by 200).
  c.on_snapshot(58800, ClockState::Running);
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  CHECK(c.last_error_ms() == 200);
  CHECK(c.sample().display_ms == 59000);  // continuous: no jump
  ts.advance_ms(1000);
  // R-1000 + (200 - 100) = 57800 + 100 = 57900
  CHECK(c.sample().display_ms == 57900);
  ts.advance_ms(1000);
  CHECK(c.sample().display_ms == 56800);  // converged after 2 s: R - 2000
  ts.advance_ms(500);
  CHECK(c.sample().display_ms == 56300);  // stays converged (still before stale timeout)
}

TEST_CASE("clock: slew never counts up (non-increasing every 10 ms)") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  // negative drift: display too low by 200 (agent says more time left)
  c.on_snapshot(59200, ClockState::Running);
  CHECK(c.last_error_ms() == -200);
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  int32_t prev = c.sample().display_ms;
  CHECK(prev == 59000);
  for (int i = 0; i < 300; ++i) {
    ts.advance_ms(10);
    int32_t d = c.sample().display_ms;
    CHECK(d <= prev);
    prev = d;
  }
  CHECK(prev == 59200 - 3000);  // converged and tracking R
}

TEST_CASE("clock: positive-drift slew non-increasing and converges") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58800, ClockState::Running);  // e = +200
  int32_t prev = c.sample().display_ms;
  for (int i = 0; i < 300; ++i) {
    ts.advance_ms(10);
    int32_t d = c.sample().display_ms;
    CHECK(d <= prev);
    prev = d;
  }
  CHECK(prev == 58800 - 3000);
}

TEST_CASE("clock: drift at exactly threshold slews, above snaps") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58750, ClockState::Running);  // e = 250
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  ts.advance_ms(1000);
  c.on_snapshot(57000, ClockState::Running);
  // before: D = 58750-1000+(250-100) = 57900; e = 900 -> snap
  CHECK(c.last_error_ms() == 900);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 57000);
}

TEST_CASE("clock: 300 ms drift snaps") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58700, ClockState::Running);  // e = +300
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == 300);
  CHECK(c.sample().display_ms == 58700);
  ts.advance_ms(500);
  CHECK(c.sample().display_ms == 58200);
  c.on_snapshot(58500, ClockState::Running);  // e = -300
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == -300);
  CHECK(c.sample().display_ms == 58500);
}

TEST_CASE("clock: state changes snap exactly") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58950, ClockState::Stopped);  // e = 50, still snap
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 58950);
  ts.advance_ms(1000);
  CHECK(c.sample().display_ms == 58950);
  c.on_snapshot(58900, ClockState::Running);  // stopped -> running
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == 50);
  CHECK(c.sample().display_ms == 58900);
  ts.advance_ms(100);
  CHECK(c.sample().display_ms == 58800);
  c.on_snapshot(58000, ClockState::Injury);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 58000);
}

TEST_CASE("clock: stale freezes at D(t_rx+timeout), colon off") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(3000);
  ClockView v = c.sample();
  CHECK_FALSE(v.stale);  // exactly at the timeout is not yet stale
  CHECK(v.colon_on);
  CHECK(v.display_ms == 57000);
  ts.advance_ms(1);
  CHECK(c.stale());
  v = c.sample();
  CHECK(v.stale);
  CHECK_FALSE(v.colon_on);
  CHECK(v.display_ms == 57000);
  CHECK(v.state == ClockState::Running);  // state itself is retained
  ts.advance_ms(10000);
  v = c.sample();
  CHECK(v.stale);
  CHECK(v.display_ms == 57000);
  CHECK_FALSE(v.colon_on);
}

TEST_CASE("clock: stale while slewing freezes at slewed value") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58800, ClockState::Running);  // slew +200
  ts.advance_ms(3500);
  // D(t_rx+3000): base 55800, offset fully decayed after 2 s
  CHECK(c.sample().display_ms == 55800);
  CHECK(c.sample().stale);
}

TEST_CASE("clock: recovery from stale always snaps") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(5000);  // frozen at 57000
  CHECK(c.sample().display_ms == 57000);
  // Agent says 56900: e = 100, which would slew if we were not stale.
  c.on_snapshot(56900, ClockState::Running);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == 100);
  ClockView v = c.sample();
  CHECK(v.display_ms == 56900);
  CHECK_FALSE(v.stale);
  CHECK(v.colon_on);
  ts.advance_ms(100);
  CHECK(c.sample().display_ms == 56800);
}

TEST_CASE("clock: clamps at 0, state stays running, never negative") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(1000, ClockState::Running);
  ts.advance_ms(1000);
  CHECK(c.sample().display_ms == 0);
  ts.advance_ms(1500);  // still within stale timeout
  ClockView v = c.sample();
  CHECK(v.display_ms == 0);
  CHECK(v.state == ClockState::Running);
  CHECK_FALSE(v.stale);
  CHECK(v.colon_on);
  ts.advance_ms(60000);
  v = c.sample();
  CHECK(v.display_ms == 0);
  CHECK(v.state == ClockState::Running);
  CHECK(v.stale);
}

TEST_CASE("clock: negative slew near zero stays non-negative") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(1000, ClockState::Running);
  ts.advance_ms(900);                          // D = 100
  c.on_snapshot(50, ClockState::Running);      // e = 50 -> slew
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  for (int i = 0; i < 100; ++i) {
    CHECK(c.sample().display_ms >= 0);
    ts.advance_ms(7);
  }
  CHECK(c.sample().display_ms == 0);
}

TEST_CASE("clock: expired shows 0 even if ms > 0; running after expired snaps") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(30000, ClockState::Running);
  ts.advance_ms(100);
  c.on_snapshot(9999, ClockState::Expired);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 0);
  ts.advance_ms(1000);
  CHECK(c.sample().display_ms == 0);
  CHECK(c.sample().state == ClockState::Expired);
  c.on_snapshot(5000, ClockState::Running);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 5000);
}

TEST_CASE("clock: injury does not interpolate") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(20000, ClockState::Injury);
  ts.advance_ms(2999);
  CHECK(c.sample().display_ms == 20000);
  CHECK(c.sample().state == ClockState::Injury);
  CHECK(c.sample().colon_on);
  ts.advance_ms(2);
  CHECK(c.sample().display_ms == 20000);
  CHECK(c.sample().stale);
  CHECK_FALSE(c.sample().colon_on);
}

TEST_CASE("clock: sample is pure (1 call vs 1000 calls)") {
  auto run = [](int calls) {
    FakeTimeSource ts;
    ClockModel c(ts);
    c.on_snapshot(60000, ClockState::Running);
    ts.advance_ms(1000);
    c.on_snapshot(58800, ClockState::Running);  // slewing
    ts.advance_ms(777);
    ClockView v;
    for (int i = 0; i < calls; ++i) v = c.sample();
    return v;
  };
  ClockView a = run(1), b = run(1000);
  CHECK(a.display_ms == b.display_ms);
  CHECK(a.state == b.state);
  CHECK(a.stale == b.stale);
  CHECK(a.colon_on == b.colon_on);
}

TEST_CASE("clock: sampling between snapshots does not alter the next decision") {
  auto run = [](bool probe) {
    FakeTimeSource ts;
    ClockModel c(ts);
    c.on_snapshot(60000, ClockState::Running);
    for (int i = 0; i < 100; ++i) {
      ts.advance_ms(10);
      if (probe) c.sample();
    }
    c.on_snapshot(58850, ClockState::Running);
    ts.advance_ms(500);
    return c.sample().display_ms;
  };
  CHECK(run(false) == run(true));
}

TEST_CASE("clock: custom config honoured") {
  FakeTimeSource ts;
  ClockConfig cfg;
  cfg.snap_threshold_ms = 100;
  cfg.slew_permille = 500;   // 500 ms of correction per second
  cfg.stale_timeout_ms = 2000;
  ClockModel c(ts, cfg);
  c.on_snapshot(60000, ClockState::Running);
  ts.advance_ms(1000);
  c.on_snapshot(58950, ClockState::Running);  // e = 50 <= 100: slew
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  ts.advance_ms(100);  // correction 50 ms: fully converged
  CHECK(c.sample().display_ms == 58850);
  c.on_snapshot(58800, ClockState::Running);  // D = 58850, e = 50 again: slew
  CHECK(c.last_sync() == ClockModel::Sync::Slew);
  c.on_snapshot(57600, ClockState::Running);  // D = 58850 (no time passed), e = 1250 > 100: snap
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.last_error_ms() == 1250);
  ts.advance_ms(2000);
  CHECK_FALSE(c.sample().stale);
  ts.advance_ms(1);
  CHECK(c.sample().stale);
  CHECK(c.sample().display_ms == 55600);

  // set_config affects subsequent decisions
  ClockConfig strict = cfg;
  strict.snap_threshold_ms = 0;
  c.set_config(strict);
  c.on_snapshot(55000, ClockState::Running);  // stale recovery: snap
  ts.advance_ms(100);
  c.on_snapshot(54910, ClockState::Running);  // D = 54900, e = -10 > 0: snap
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
}

TEST_CASE("clock: reset clears data") {
  FakeTimeSource ts;
  ClockModel c(ts);
  c.on_snapshot(1000, ClockState::Running);
  c.reset();
  CHECK_FALSE(c.has_data());
  CHECK(c.sample().display_ms == 0);
  CHECK_FALSE(c.sample().colon_on);
  c.on_snapshot(2000, ClockState::Running);
  CHECK(c.last_sync() == ClockModel::Sync::Snap);
  CHECK(c.sample().display_ms == 2000);
}
