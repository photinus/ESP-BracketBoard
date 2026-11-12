#include "bb/auth.h"
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace bb {

bool token_equal(const char* expected, const char* presented) {
  const size_t el = expected ? std::strlen(expected) : 0;
  const size_t pl = presented ? std::strlen(presented) : 0;
  const size_t n = el > pl ? el : pl;

  // Accumulate differences over max(len) bytes with no early exit. Bytes past the end
  // of a string read as 0, and a length mismatch is folded into the accumulator.
  uint32_t diff = static_cast<uint32_t>(el ^ pl);
  for (size_t i = 0; i < n; ++i) {
    const uint8_t a = i < el ? static_cast<uint8_t>(expected[i]) : 0;
    const uint8_t b = i < pl ? static_cast<uint8_t>(presented[i]) : 0;
    diff |= static_cast<uint32_t>(a ^ b);
  }
  // Null or empty expected never matches, even against an empty presented token.
  const bool expected_ok = el != 0;
  return expected_ok && diff == 0;
}

}  // namespace bb
