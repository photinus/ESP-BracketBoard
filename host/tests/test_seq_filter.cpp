#include <string>

#include "doctest.h"
#include "bb/seq_filter.h"

using bb::SeqFilter;
using bb::SeqVerdict;

TEST_CASE("seq: first offer is AcceptNewSession and commits") {
  SeqFilter f;
  CHECK_FALSE(f.has_last());
  CHECK(f.offer("a", 7) == SeqVerdict::AcceptNewSession);
  CHECK(f.has_last());
  CHECK(f.last_seq() == 7);
  CHECK(std::string(f.session()) == "a");
}

TEST_CASE("seq: increasing accepted, equal duplicate, lower stale") {
  SeqFilter f;
  f.offer("a", 10);
  CHECK(f.offer("a", 11) == SeqVerdict::Accept);
  CHECK(f.offer("a", 15) == SeqVerdict::Accept);  // gaps are fine
  CHECK(f.last_seq() == 15);
  CHECK(f.offer("a", 15) == SeqVerdict::Duplicate);
  CHECK(f.offer("a", 14) == SeqVerdict::Stale);
  CHECK(f.offer("a", 0) == SeqVerdict::Stale);
  CHECK(f.last_seq() == 15);  // duplicate/stale do not change state
  CHECK(f.offer("a", 16) == SeqVerdict::Accept);
}

TEST_CASE("seq: wraparound at 2^32") {
  SeqFilter f;
  CHECK(f.offer("a", 0xFFFFFFFEu) == SeqVerdict::AcceptNewSession);
  CHECK(f.offer("a", 0xFFFFFFFFu) == SeqVerdict::Accept);
  CHECK(f.offer("a", 0u) == SeqVerdict::Accept);
  CHECK(f.offer("a", 1u) == SeqVerdict::Accept);
  CHECK(f.last_seq() == 1);
  CHECK(f.offer("a", 0xFFFFFFFFu) == SeqVerdict::Stale);  // now behind across the wrap
  CHECK(f.offer("a", 1u) == SeqVerdict::Duplicate);
}

TEST_CASE("seq: RFC1982 half-range boundary") {
  // delta = (int32_t)(seq - last). delta in [1, 0x7FFFFFFF] is ahead; delta == 0x80000000
  // wraps to INT32_MIN, which is negative, so it is treated as Stale (deliberate, per header).
  SeqFilter f;
  f.offer("a", 100);
  CHECK(f.offer("a", 100u + 0x80000000u) == SeqVerdict::Stale);
  CHECK(f.last_seq() == 100);
  // The largest forward jump is accepted.
  CHECK(f.offer("a", 100u + 0x7FFFFFFFu) == SeqVerdict::Accept);
  CHECK(f.last_seq() == 100u + 0x7FFFFFFFu);

  // A seq 0x7FFFFFFF behind the last (delta = -0x7FFFFFFF) is Stale...
  SeqFilter g;
  g.offer("a", 0x90000000u);
  CHECK(g.offer("a", 0x90000000u - 0x7FFFFFFFu) == SeqVerdict::Stale);
  // ...and so is 0x80000000 behind (same point on the ring as 0x80000000 ahead).
  CHECK(g.offer("a", 0x90000000u - 0x80000000u) == SeqVerdict::Stale);
  // 0x80000001 behind is delta = +0x7FFFFFFF: considered ahead.
  CHECK(g.offer("a", 0x90000000u - 0x80000001u) == SeqVerdict::Accept);
}

TEST_CASE("seq: new session takes over immediately") {
  SeqFilter f;
  f.offer("old", 5000);
  CHECK(f.offer("new", 1) == SeqVerdict::AcceptNewSession);
  CHECK(std::string(f.session()) == "new");
  CHECK(f.last_seq() == 1);
  CHECK(f.offer("new", 2) == SeqVerdict::Accept);
  // Old session is now just a different session: it takes over again (documented v1 limit).
  CHECK(f.offer("old", 5001) == SeqVerdict::AcceptNewSession);
}

TEST_CASE("seq: reset makes next offer a new session") {
  SeqFilter f;
  f.offer("a", 50);
  f.reset();
  CHECK_FALSE(f.has_last());
  CHECK(f.offer("a", 3) == SeqVerdict::AcceptNewSession);  // even lower seq, same session
  f.reset();
  CHECK(f.offer("b", 0xFFFFFFFFu) == SeqVerdict::AcceptNewSession);
}

TEST_CASE("seq: session of max length is kept intact, prefix differs") {
  SeqFilter f;
  const char* s16 = "abcdefghijklmnop";
  CHECK(f.offer(s16, 1) == SeqVerdict::AcceptNewSession);
  CHECK(std::string(f.session()) == s16);
  CHECK(f.offer(s16, 2) == SeqVerdict::Accept);
  CHECK(f.offer("abcdefghijklmno", 1) == SeqVerdict::AcceptNewSession);
}
