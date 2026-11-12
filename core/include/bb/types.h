// Shared value types for the BracketBoard snapshot protocol (v1).
// Portable C++17: no ESP-IDF or OS headers.
#pragma once
#include <cstddef>
#include <cstdint>

namespace bb {

enum class ClockState : uint8_t { Running, Stopped, Injury, Expired };
enum class Screen : uint8_t { Score, Splash };

constexpr int kProtocolVersion = 1;
constexpr size_t kMaxMessageBytes = 1024;
constexpr size_t kSessionMax = 16;  // chars, excluding NUL
constexpr size_t kLabelMax = 8;     // weight / bout, excluding NUL

// Full state snapshot, as received from the agent. All fields required.
struct Snapshot {
  uint8_t v = kProtocolVersion;
  char session[kSessionMax + 1] = {0};
  uint32_t seq = 0;
  int64_t sender_ts = 0;  // agent wall clock, Unix ms. Diagnostics only.
  uint8_t left_score = 0, right_score = 0;
  uint8_t left_warnings = 0, right_warnings = 0;
  uint8_t period = 1;
  int32_t clock_ms_remaining = 0;
  ClockState clock_state = ClockState::Stopped;
  Screen screen = Screen::Score;
  char weight[kLabelMax + 1] = {0};
  char bout[kLabelMax + 1] = {0};
  bool colors_swapped = false;
};

const char* clock_state_name(ClockState s);  // "running" | "stopped" | "injury" | "expired"
const char* screen_name(Screen s);           // "score" | "splash"

}  // namespace bb
