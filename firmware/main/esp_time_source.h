// bb::TimeSource backed by the ESP-IDF high-resolution monotonic timer.
#pragma once
#include <cstdint>
#include "bb/time_source.h"
#include "esp_timer.h"

namespace bbfw {

class EspTimeSource final : public bb::TimeSource {
 public:
  int64_t now_us() const override { return esp_timer_get_time(); }
};

}  // namespace bbfw
