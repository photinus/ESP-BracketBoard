#include "bb/frame.h"

namespace bb {

void format_clock(int32_t ms, char out[6]) {
  if (ms < 0) ms = 0;
  // Whole seconds, rounded up. Computed in 64 bits so ms near INT32_MAX cannot overflow.
  int64_t secs = (static_cast<int64_t>(ms) + 999) / 1000;
  if (secs > 5999) secs = 5999;  // 99:59 is the largest representable value
  const int mins = static_cast<int>(secs / 60);
  const int s = static_cast<int>(secs % 60);
  char* p = out;
  if (mins >= 10) *p++ = static_cast<char>('0' + mins / 10);
  *p++ = static_cast<char>('0' + mins % 10);
  *p++ = ':';
  *p++ = static_cast<char>('0' + s / 10);
  *p++ = static_cast<char>('0' + s % 10);
  *p = '\0';
}

}  // namespace bb
