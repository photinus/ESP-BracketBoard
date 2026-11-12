#include <cstring>
#include <sstream>
#include <string>
#include "bb/frame.h"
#include "console_display.h"
#include "doctest.h"

using namespace bb;

static Frame base_frame() {
  Frame f;
  f.screen = Screen::Score;
  std::strcpy(f.clock, "1:27");
  f.colon_on = true;
  f.waiting = false;
  f.left_score = 3;
  f.right_score = 1;
  f.left_warnings = 0;
  f.right_warnings = 1;
  f.period = 2;
  f.clock_state = ClockState::Running;
  std::strcpy(f.weight, "157");
  std::strcpy(f.bout, "1203");
  return f;
}

TEST_CASE("console display: normal frame golden") {
  CHECK(ConsoleDisplay::to_string(base_frame()) ==
        "W:157 BOUT:1203 P2\n"
        "CLOCK 1:27 running\n"
        "L  3 w0 | R  1 w1");
}

TEST_CASE("console display: colon off") {
  Frame f = base_frame();
  f.colon_on = false;
  CHECK(ConsoleDisplay::to_string(f) ==
        "W:157 BOUT:1203 P2\n"
        "CLOCK 1 27 running\n"
        "L  3 w0 | R  1 w1");
}

TEST_CASE("console display: stale frame golden") {
  Frame f = base_frame();
  f.stale = true;
  f.colon_on = false;
  CHECK(ConsoleDisplay::to_string(f) ==
        "W:157 BOUT:1203 P2 STALE\n"
        "CLOCK 1 27 running\n"
        "L  3 w0 | R  1 w1");
}

TEST_CASE("console display: waiting frame golden") {
  Frame f;  // defaults: no data
  CHECK(ConsoleDisplay::to_string(f) ==
        "W:- BOUT:- P1 WAITING SPLASH\n"
        "CLOCK - -- stopped\n"
        "L  0 w0 | R  0 w0");
}

TEST_CASE("console display: manual frame golden") {
  Frame f = base_frame();
  f.manual = true;
  f.colors_swapped = true;
  f.left_score = 12;
  f.right_score = 99;
  f.left_warnings = 9;
  f.clock_state = ClockState::Injury;
  CHECK(ConsoleDisplay::to_string(f) ==
        "W:157 BOUT:1203 P2 MANUAL SWAP\n"
        "CLOCK 1:27 injury\n"
        "L 12 w9 | R 99 w1");
}

TEST_CASE("console display: render writes to_string plus newline, begin prints nothing") {
  std::ostringstream os;
  ConsoleDisplay d(os);
  d.begin();
  CHECK(os.str().empty());
  Frame f = base_frame();
  d.render(f);
  CHECK(os.str() == ConsoleDisplay::to_string(f) + "\n");
}
