// PanelDisplay: bb::Display backend for the physical sign.
//
// !!! TODO(HARDWARE): the panel is not chosen yet. This is a STUB that only logs the Frame
// !!! at 1 Hz. Replace begin()/render() with the real driver (HUB75 / LED matrix / TFT ...).
// !!! render() is called at ~20 Hz from the render task: it must not block for long.
#pragma once
#include <cstdint>
#include "bb/display.h"
#include "bb/frame.h"

namespace bbfw {

class PanelDisplay final : public bb::Display {
 public:
  void begin() override;
  void render(const bb::Frame& f) override;

  // Setup overlay: while non-null, a real driver should show "token: <value>" on the
  // splash screen (first 60 s after a first-boot token generation, while the AP is up).
  // Pass nullptr to clear. Only call from the render task.
  void set_setup_overlay(const char* token_or_null);

 private:
  int64_t last_log_us_ = 0;
  bool overlay_ = false;
  char overlay_token_[40] = {0};  // for the real driver; never logged
};

}  // namespace bbfw
