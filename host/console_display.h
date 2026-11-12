// ASCII Display backend for host tools and tests. TODO(worker 3): implement.
#pragma once
#include <ostream>
#include <string>
#include "bb/display.h"

namespace bb {
class ConsoleDisplay : public Display {
 public:
  explicit ConsoleDisplay(std::ostream& os) : os_(os) {}
  void begin() override;
  void render(const Frame& f) override;
  // Deterministic text rendering of a frame (used by golden-string tests).
  static std::string to_string(const Frame& f);
 private:
  std::ostream& os_;
};
}  // namespace bb
