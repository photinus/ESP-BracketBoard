#pragma once

namespace bb {

// Constant-time, length-safe comparison. Null or empty `expected` never matches.
bool token_equal(const char* expected, const char* presented);

}  // namespace bb
