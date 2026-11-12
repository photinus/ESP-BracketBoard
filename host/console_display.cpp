#include "console_display.h"
#include <cstdio>
#include <cstring>

namespace bb {

void ConsoleDisplay::begin() {}

void ConsoleDisplay::render(const Frame& f) { os_ << to_string(f) << '\n'; }

// Format (3 lines, no trailing newline):
//   W:<weight> BOUT:<bout> P<period>[ MANUAL][ STALE][ WAITING][ SPLASH][ SWAP]
//   CLOCK <M:SS or "M SS" when the colon is off> <clock_state>
//   L <score> w<warnings> | R <score> w<warnings>
std::string ConsoleDisplay::to_string(const Frame& f) {
  std::string out;
  out += "W:";
  out += f.weight[0] ? f.weight : "-";
  out += " BOUT:";
  out += f.bout[0] ? f.bout : "-";
  out += " P";
  out += std::to_string(static_cast<int>(f.period));
  if (f.manual) out += " MANUAL";
  if (f.stale) out += " STALE";
  if (f.waiting) out += " WAITING";
  if (f.screen == Screen::Splash) out += " SPLASH";
  if (f.colors_swapped) out += " SWAP";
  out += '\n';

  char clk[8];
  std::strncpy(clk, f.clock, 5);
  clk[5] = '\0';
  if (!f.colon_on) {
    for (char* p = clk; *p; ++p) {
      if (*p == ':') *p = ' ';
    }
  }
  out += "CLOCK ";
  out += clk;
  out += ' ';
  out += clock_state_name(f.clock_state);
  out += '\n';

  char line[64];
  std::snprintf(line, sizeof line, "L %2d w%d | R %2d w%d", f.left_score, f.left_warnings,
                f.right_score, f.right_warnings);
  out += line;
  return out;
}

}  // namespace bb
