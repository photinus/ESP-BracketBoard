// Local clock interpolation. See docs/protocol-v1.md section 6.
// All math is int64 microseconds / integer ms: no floating point, so results are exact and
// sample() is a pure closed-form function of (stored snapshot, now).
#include "bb/clock_model.h"

namespace bb {

namespace {
constexpr int64_t kUsPerMs = 1000;
}

ClockModel::ClockModel(const TimeSource& ts, ClockConfig cfg) : ts_(ts), cfg_(cfg) {}

void ClockModel::reset() {
  has_data_ = false;
  t_rx_us_ = 0;
  remaining_ms_ = 0;
  state_ = ClockState::Stopped;
  off0_ms_ = 0;
  last_sync_ = Sync::Snap;
  last_error_ms_ = 0;
}

bool ClockModel::stale() const {
  if (!has_data_) return false;
  return ts_.now_us() - t_rx_us_ > static_cast<int64_t>(cfg_.stale_timeout_ms) * kUsPerMs;
}

// D(t) for t >= t_rx, ignoring staleness (callers clamp t to the stale horizon).
int32_t ClockModel::display_at(int64_t t_us) const {
  if (!has_data_ || state_ == ClockState::Expired) return 0;
  int64_t el_us = t_us - t_rx_us_;
  if (el_us < 0) el_us = 0;

  int64_t base_us = static_cast<int64_t>(remaining_ms_) * kUsPerMs;
  if (state_ == ClockState::Running) base_us -= el_us;

  int64_t off_us = 0;
  if (off0_ms_ != 0) {
    const int64_t rate = cfg_.slew_permille > 0 ? cfg_.slew_permille : 0;
    const int64_t mag_us = static_cast<int64_t>(off0_ms_ < 0 ? -static_cast<int64_t>(off0_ms_) : off0_ms_) * kUsPerMs;
    // correction in us = permille * elapsed_ms / 1000 = permille * elapsed_us / 1000
    int64_t left = mag_us - rate * el_us / 1000;
    if (left < 0) left = 0;
    off_us = off0_ms_ < 0 ? -left : left;
  }

  int64_t d_us = base_us + off_us;
  if (d_us < 0) d_us = 0;
  int64_t d_ms = d_us / kUsPerMs;
  if (d_ms > INT32_MAX) d_ms = INT32_MAX;
  return static_cast<int32_t>(d_ms);
}

void ClockModel::on_snapshot(int32_t clock_ms_remaining, ClockState st) {
  const int64_t now = ts_.now_us();
  const bool had = has_data_;
  const bool was_stale = stale();
  const ClockState prev_state = state_;

  int32_t d_prev = 0;
  if (had) {
    const int64_t horizon = t_rx_us_ + static_cast<int64_t>(cfg_.stale_timeout_ms) * kUsPerMs;
    d_prev = display_at(now < horizon ? now : horizon);
  }
  const int32_t e = had ? d_prev - clock_ms_remaining : 0;

  const bool can_slew = had && !was_stale && prev_state == ClockState::Running &&
                        st == ClockState::Running &&
                        (e <= cfg_.snap_threshold_ms && -e <= cfg_.snap_threshold_ms);

  last_error_ms_ = e;
  last_sync_ = can_slew ? Sync::Slew : Sync::Snap;
  off0_ms_ = can_slew ? e : 0;
  t_rx_us_ = now;
  remaining_ms_ = clock_ms_remaining;
  state_ = st;
  has_data_ = true;
}

ClockView ClockModel::sample() const {
  ClockView v;
  if (!has_data_) return v;
  const int64_t now = ts_.now_us();
  const int64_t horizon = t_rx_us_ + static_cast<int64_t>(cfg_.stale_timeout_ms) * kUsPerMs;
  const bool is_stale = now - t_rx_us_ > static_cast<int64_t>(cfg_.stale_timeout_ms) * kUsPerMs;
  v.display_ms = display_at(is_stale ? horizon : now);
  v.state = state_;
  v.stale = is_stale;
  v.colon_on = !is_stale;
  return v;
}

}  // namespace bb
