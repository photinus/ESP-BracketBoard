#include <cstdint>
#include <cstring>
#include <string>
#include "bb/frame.h"
#include "doctest.h"

static std::string fc(int32_t ms) {
  char buf[6];
  bb::format_clock(ms, buf);
  return buf;
}

TEST_CASE("format_clock rounds up to whole seconds") {
  CHECK(fc(0) == "0:00");
  CHECK(fc(1) == "0:01");
  CHECK(fc(999) == "0:01");
  CHECK(fc(1000) == "0:01");
  CHECK(fc(1001) == "0:02");
  CHECK(fc(59999) == "1:00");
  CHECK(fc(60000) == "1:00");
  CHECK(fc(60001) == "1:01");
  CHECK(fc(87340) == "1:28");
  CHECK(fc(120000) == "2:00");
  CHECK(fc(599000) == "9:59");
  CHECK(fc(600000) == "10:00");
  CHECK(fc(3600000) == "60:00");
}

TEST_CASE("format_clock clamps") {
  CHECK(fc(-1) == "0:00");
  CHECK(fc(-100000) == "0:00");
  CHECK(fc(INT32_MIN) == "0:00");
  CHECK(fc(5999000) == "99:59");
  CHECK(fc(6000000) == "99:59");
  CHECK(fc(INT32_MAX) == "99:59");
}

TEST_CASE("format_clock always NUL terminated within 6 bytes") {
  const int32_t vals[] = {0, 1, 59999, 600000, 5999999, 6000000, INT32_MAX, -5};
  for (int32_t v : vals) {
    char buf[6];
    std::memset(buf, 'x', sizeof buf);
    bb::format_clock(v, buf);
    size_t n = 0;
    while (n < 6 && buf[n]) ++n;
    CHECK(n < 6);
    CHECK(n >= 4);
  }
}
