// Manual mode: portable authoritative clock + score kept ON the device.
// Only used when manual mode is enabled AND no agent is live. See docs/architecture.md.
#pragma once
#include <cstddef>
#include <cstdint>
#include "bb/time_source.h"
#include "bb/types.h"

namespace bb {

class ManualController {
 public:
  explicit ManualController(const TimeSource& ts);

  // JSON command: {"cmd": <name>, ...}. Returns true if applied, false if malformed/unknown.
  //   {"cmd":"clock_start"}  {"cmd":"clock_stop"}  {"cmd":"clock_injury"}
  //   {"cmd":"clock_set","ms":120000}  {"cmd":"clock_adjust","delta_ms":-1000}
  //   {"cmd":"score","side":"left"|"right","delta":2}   (clamped 0..99)
  //   {"cmd":"warning","side":"left"|"right","delta":1} (clamped 0..9)
  //   {"cmd":"period","value":2}                         (1..9)
  //   {"cmd":"weight","value":"157"} {"cmd":"bout","value":"1203"}
  //   {"cmd":"swap"} {"cmd":"splash","value":true|false} {"cmd":"reset"}
  bool apply(const char* json_cmd, size_t len);

  // Current state, computed from the local monotonic clock (the clock expires at 0 itself).
  // session is "manual", seq increments on each applied command.
  Snapshot current() const;

 private:
  const TimeSource& ts_;
  Snapshot base_;          // state at `anchor_us_`
  int64_t anchor_us_ = 0;
};

}  // namespace bb
