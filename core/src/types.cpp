#include "bb/types.h"

namespace bb {

const char* clock_state_name(ClockState s) {
  switch (s) {
    case ClockState::Running: return "running";
    case ClockState::Stopped: return "stopped";
    case ClockState::Injury: return "injury";
    case ClockState::Expired: return "expired";
  }
  return "stopped";
}

const char* screen_name(Screen s) { return s == Screen::Splash ? "splash" : "score"; }

}  // namespace bb
