// Board: owns the authoritative-for-display state. Single entry point for all ingest.
// NOT thread-safe: callers (firmware glue) serialise with one mutex.
#pragma once
#include <cstddef>
#include <cstdint>
#include "bb/clock_model.h"
#include "bb/frame.h"
#include "bb/manual.h"
#include "bb/seq_filter.h"
#include "bb/snapshot.h"
#include "bb/time_source.h"

namespace bb {

enum class Source : uint8_t { Ws, Http };

struct IngestResult {
  enum Kind : uint8_t { Applied, Duplicate, Stale, Invalid, ManualLocked } kind = Invalid;
  ParseResult parse;  // meaningful when kind == Invalid
  uint32_t seq = 0;   // seq of the offered snapshot when it parsed
};

struct BoardConfig {
  ClockConfig clock;
  bool manual_enabled = false;  // default OFF
};

class Board {
 public:
  Board(const TimeSource& ts, BoardConfig cfg = {});

  // Parse -> SeqFilter -> (Applied) store fields + ClockModel::on_snapshot + liveness refresh.
  // Agent snapshots are ALWAYS accepted regardless of manual mode (agent is source of truth).
  // Duplicate/Stale/Invalid do not refresh liveness. Never returns ManualLocked
  // (that result is produced by ControlHub for /manual/cmd).
  IngestResult ingest_state(const char* json, size_t len, Source src);

  bool agent_live() const;     // an accepted snapshot arrived within clock.stale_timeout_ms
  bool manual_active() const;  // manual_enabled && !agent_live()
  void set_manual_enabled(bool on) { cfg_.manual_enabled = on; }
  bool manual_enabled() const { return cfg_.manual_enabled; }

  ManualController& manual() { return manual_; }

  // Display frame. Source = manual if manual_active(), else the last agent snapshot (with
  // ClockModel interpolation; stale => colon off). waiting=true until any data exists.
  Frame frame() const;

  const SeqFilter& seq() const { return seq_; }
  const ClockModel& clock() const { return clock_; }
  bool has_agent_data() const { return has_snapshot_; }

 private:
  const TimeSource& ts_;
  BoardConfig cfg_;
  SeqFilter seq_;
  ClockModel clock_;
  ManualController manual_;
  Snapshot last_;
  bool has_snapshot_ = false;
  int64_t last_accept_us_ = 0;
};

}  // namespace bb
