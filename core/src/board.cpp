// Board: single ingest point for agent snapshots + frame composition.
//
// Liveness: the agent is live iff a snapshot has been ACCEPTED and no more than
// clock.stale_timeout_ms has elapsed since. Strictness matches ClockModel::stale() exactly
// (stale iff elapsed is strictly greater than the timeout), so agent_live() == !clock.stale()
// whenever agent data exists.
#include "bb/board.h"
#include <cstring>

namespace bb {

Board::Board(const TimeSource& ts, BoardConfig cfg)
    : ts_(ts), cfg_(cfg), seq_(), clock_(ts, cfg.clock), manual_(ts) {}

IngestResult Board::ingest_state(const char* json, size_t len, Source /*src*/) {
  IngestResult r;
  Snapshot snap;
  r.parse = parse_snapshot(json, len, snap);
  if (!r.parse.ok()) {
    r.kind = IngestResult::Invalid;
    return r;
  }
  r.seq = snap.seq;
  switch (seq_.offer(snap.session, snap.seq)) {
    case SeqVerdict::Duplicate:
      r.kind = IngestResult::Duplicate;
      return r;
    case SeqVerdict::Stale:
      r.kind = IngestResult::Stale;
      return r;
    case SeqVerdict::Accept:
    case SeqVerdict::AcceptNewSession:
      break;
  }
  last_ = snap;
  has_snapshot_ = true;
  last_accept_us_ = ts_.now_us();
  clock_.on_snapshot(snap.clock_ms_remaining, snap.clock_state);
  r.kind = IngestResult::Applied;
  return r;
}

bool Board::agent_live() const {
  if (!has_snapshot_) return false;
  const int64_t limit = static_cast<int64_t>(cfg_.clock.stale_timeout_ms) * 1000;
  return ts_.now_us() - last_accept_us_ <= limit;
}

bool Board::manual_active() const { return cfg_.manual_enabled && !agent_live(); }

Frame Board::frame() const {
  Frame f;
  if (manual_active()) {
    const Snapshot m = manual_.current();
    f.screen = m.screen;
    format_clock(m.clock_ms_remaining, f.clock);
    f.colon_on = true;
    f.stale = false;
    f.left_score = m.left_score;
    f.right_score = m.right_score;
    f.left_warnings = m.left_warnings;
    f.right_warnings = m.right_warnings;
    f.period = m.period;
    f.clock_state = m.clock_state;
    std::memcpy(f.weight, m.weight, sizeof f.weight);
    std::memcpy(f.bout, m.bout, sizeof f.bout);
    f.colors_swapped = m.colors_swapped;
    f.waiting = false;
    f.manual = true;
    return f;
  }
  if (has_snapshot_) {
    const ClockView v = clock_.sample();
    f.screen = last_.screen;
    format_clock(v.display_ms, f.clock);
    f.colon_on = v.colon_on;
    f.stale = v.stale;
    f.left_score = last_.left_score;
    f.right_score = last_.right_score;
    f.left_warnings = last_.left_warnings;
    f.right_warnings = last_.right_warnings;
    f.period = last_.period;
    f.clock_state = v.state;  // last accepted state is kept while stale
    std::memcpy(f.weight, last_.weight, sizeof f.weight);
    std::memcpy(f.bout, last_.bout, sizeof f.bout);
    f.colors_swapped = last_.colors_swapped;
    f.waiting = false;
    f.manual = false;
    return f;
  }
  // No data from any source yet: default splash-like frame ("-:--", waiting).
  return f;
}

}  // namespace bb
