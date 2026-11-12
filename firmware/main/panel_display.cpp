#include "panel_display.h"

#include <cstring>

#include "esp_log.h"
#include "esp_timer.h"

namespace bbfw {

namespace {
const char* const TAG = "bb_panel";
constexpr int64_t kLogPeriodUs = 1000 * 1000;
}  // namespace

void PanelDisplay::begin() {
  // TODO(HARDWARE): initialise the real panel driver here (GPIO/I2S/SPI setup, framebuffer,
  // brightness). Hardware is not decided yet, so nothing is drawn.
  ESP_LOGW(TAG, "STUB display: frames are only logged at 1 Hz (no panel driver yet)");
  last_log_us_ = 0;
}

void PanelDisplay::set_setup_overlay(const char* token_or_null) {
  if (token_or_null) {
    strlcpy(overlay_token_, token_or_null, sizeof(overlay_token_));
    overlay_ = true;
  } else {
    overlay_token_[0] = '\0';
    overlay_ = false;
  }
}

void PanelDisplay::render(const bb::Frame& f) {
  // TODO(HARDWARE): draw `f` on the panel (and the overlay token when overlay_ is set and
  // the splash/waiting screen is showing). Keep this non-blocking: ~20 Hz caller.
  const int64_t now = esp_timer_get_time();
  if (last_log_us_ != 0 && now - last_log_us_ < kLogPeriodUs) return;
  last_log_us_ = now;

  ESP_LOGI(TAG,
           "screen=%s clock=%s colon=%d L=%u R=%u warn=%u/%u per=%u state=%s wt='%s' bout='%s' "
           "swap=%d stale=%d waiting=%d manual=%d%s",
           bb::screen_name(f.screen), f.clock, f.colon_on, f.left_score, f.right_score,
           f.left_warnings, f.right_warnings, f.period, bb::clock_state_name(f.clock_state),
           f.weight, f.bout, f.colors_swapped, f.stale, f.waiting, f.manual,
           overlay_ ? " [setup overlay active]" : "");
}

}  // namespace bbfw
