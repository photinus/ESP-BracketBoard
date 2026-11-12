// Stale / duplicate snapshot filter. See docs/protocol-v1.md "Seq rules".
#include "bb/seq_filter.h"

#include <cstring>

#include "bb/types.h"

namespace bb {

SeqVerdict SeqFilter::offer(const char* session, uint32_t seq) {
  const char* s = session ? session : "";
  if (!has_last_ || std::strncmp(session_, s, kSessionMax + 1) != 0) {
    std::strncpy(session_, s, kSessionMax);
    session_[kSessionMax] = '\0';
    last_seq_ = seq;
    has_last_ = true;
    return SeqVerdict::AcceptNewSession;
  }
  // RFC 1982 serial arithmetic; unsigned subtraction wraps, then reinterpret as signed.
  const int32_t delta = static_cast<int32_t>(static_cast<uint32_t>(seq - last_seq_));
  if (delta > 0) {
    last_seq_ = seq;
    return SeqVerdict::Accept;
  }
  return delta == 0 ? SeqVerdict::Duplicate : SeqVerdict::Stale;
}

void SeqFilter::reset() {
  has_last_ = false;
  session_[0] = '\0';
  last_seq_ = 0;
}

}  // namespace bb
