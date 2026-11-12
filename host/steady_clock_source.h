// Host TimeSource backed by std::chrono::steady_clock.
#pragma once
#include <chrono>
#include "bb/time_source.h"

namespace bb {
class SteadyTimeSource : public TimeSource {
 public:
  int64_t now_us() const override {
    using namespace std::chrono;
    return duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count();
  }
};
}  // namespace bb
