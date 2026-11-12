// Local clock interpolation for agent mode. See docs/protocol-v1.md "Clock model".
// The sign never decides expiry or start/stop: it only extrapolates between snapshots.
#pragma once
#include <cstdint>
#include "bb/time_source.h"
#include "bb/types.h"

namespace bb {

struct ClockConfig {
  int32_t snap_threshold_ms = 250;  // |drift| above this snaps, otherwise slews
  int32_t slew_permille = 100;      // correction rate: 100 = 100 ms per second of elapsed time
  int32_t stale_timeout_ms = 3000;  // no snapshot for this long => freeze + stale
};

struct ClockView {
  int32_t display_ms = 0;  // >= 0
  ClockState state = ClockState::Stopped;
  bool stale = false;
  bool colon_on = false;   // false when stale or no data yet
};

class ClockModel {
 public:
  enum class Sync : uint8_t { Snap, Slew };

  explicit ClockModel(const TimeSource& ts, ClockConfig cfg = {});

  // Call at receipt of each ACCEPTED snapshot.
  void on_snapshot(int32_t clock_ms_remaining, ClockState st);

  // Pure function of (stored snapshot, now): same result however often it is called.
  ClockView sample() const;

  bool has_data() const { return has_data_; }
  bool stale() const;
  Sync last_sync() const { return last_sync_; }
  int32_t last_error_ms() const { return last_error_ms_; }  // drift e at last on_snapshot
  void reset();
  void set_config(const ClockConfig& c) { cfg_ = c; }

 private:
  int32_t display_at(int64_t t_us) const;  // D(t), unclamped time domain handled inside

  const TimeSource& ts_;
  ClockConfig cfg_;
  bool has_data_ = false;
  int64_t t_rx_us_ = 0;
  int32_t remaining_ms_ = 0;
  ClockState state_ = ClockState::Stopped;
  int32_t off0_ms_ = 0;
  Sync last_sync_ = Sync::Snap;
  int32_t last_error_ms_ = 0;
};

}  // namespace bb
