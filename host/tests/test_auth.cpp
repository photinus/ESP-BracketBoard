#include "bb/auth.h"
#include "doctest.h"

TEST_CASE("token_equal") {
  CHECK(bb::token_equal("secret", "secret"));
  CHECK_FALSE(bb::token_equal("secret", "secreT"));
  CHECK_FALSE(bb::token_equal("secret", "secre"));     // presented is a prefix
  CHECK_FALSE(bb::token_equal("secret", "secrets"));   // presented longer
  CHECK_FALSE(bb::token_equal("secret", ""));
  CHECK_FALSE(bb::token_equal("secret", nullptr));
  CHECK_FALSE(bb::token_equal("", "secret"));
  CHECK_FALSE(bb::token_equal(nullptr, "secret"));
  CHECK_FALSE(bb::token_equal("", ""));
  CHECK_FALSE(bb::token_equal(nullptr, nullptr));
  CHECK_FALSE(bb::token_equal("", nullptr));
  CHECK(bb::token_equal("a", "a"));
  CHECK_FALSE(bb::token_equal("a", "b"));
}
