// Display backend interface. Host: ConsoleDisplay. Firmware: PanelDisplay (TODO hardware).
#pragma once
#include "bb/frame.h"

namespace bb {

class Display {
 public:
  virtual ~Display() = default;
  virtual void begin() = 0;
  virtual void render(const Frame& f) = 0;  // called ~20 Hz; must not block long
};

}  // namespace bb
