// Stale / duplicate snapshot filter. See docs/protocol-v1.md "Seq rules".
#pragma once
#include <cstdint>

namespace bb {

enum class SeqVerdict : uint8_t { Accept, AcceptNewSession, Duplicate, Stale };

class SeqFilter {
 public:
  // If no previous epoch, or `session` differs from the current one: commit and
  // return AcceptNewSession. Otherwise RFC 1982 serial arithmetic on seq:
  // delta = (int32_t)(seq - last_seq); >0 Accept (commit), ==0 Duplicate, <0 Stale.
  // Duplicate/Stale do not change state.
  SeqVerdict offer(const char* session, uint32_t seq);
  void reset();  // as after reboot: next offer is always AcceptNewSession
  bool has_last() const { return has_last_; }
  const char* session() const { return session_; }
  uint32_t last_seq() const { return last_seq_; }

 private:
  bool has_last_ = false;
  char session_[17] = {0};
  uint32_t last_seq_ = 0;
};

}  // namespace bb
