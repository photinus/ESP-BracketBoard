// Injectable monotonic clock. Firmware: esp_timer_get_time(). Host: steady_clock.
#pragma once
#include <cstdint>

namespace bb {

struct TimeSource {
  virtual ~TimeSource() = default;
  virtual int64_t now_us() const = 0;  // monotonic, microseconds
};

// Deterministic source for tests.
class FakeTimeSource : public TimeSource {
 public:
  int64_t t = 0;
  int64_t now_us() const override { return t; }
  void advance_ms(int64_t ms) { t += ms * 1000; }
};

}  // namespace bb
