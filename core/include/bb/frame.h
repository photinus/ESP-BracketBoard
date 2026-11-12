// Display-ready frame produced by Board and consumed by a Display backend.
#pragma once
#include <cstdint>
#include "bb/types.h"

namespace bb {

struct Frame {
  Screen screen = Screen::Splash;
  char clock[6] = "-:--";      // "M:SS" or "MM:SS"; "-:--" before any data
  bool colon_on = false;
  bool stale = false;
  uint8_t left_score = 0, right_score = 0;
  uint8_t left_warnings = 0, right_warnings = 0;
  uint8_t period = 1;
  ClockState clock_state = ClockState::Stopped;
  char weight[kLabelMax + 1] = {0};
  char bout[kLabelMax + 1] = {0};
  bool colors_swapped = false;
  bool waiting = true;         // no data from any source yet
  bool manual = false;         // frame comes from the on-device manual controller
};

// Whole seconds, rounded UP: 0->"0:00", 1..1000->"0:01", 1001->"0:02", 59999->"1:00",
// 120000->"2:00", 600000->"10:00". Negative treated as 0. Values >= 6000000 ms
// (100 min) render as "99:59".
void format_clock(int32_t ms, char out[6]);

}  // namespace bb
