// Snapshot parsing / validation. See docs/protocol-v1.md section 2.
#pragma once
#include <cstddef>
#include "bb/types.h"

namespace bb {

enum class ParseError : uint8_t {
  Ok,
  TooLarge,            // len > kMaxMessageBytes
  NotJson,             // unparsable / truncated
  NotObject,           // top level is not a JSON object
  WrongType,           // "type" present but != "state" (or missing "type")
  UnsupportedVersion,  // "v" is an int but != 1
  MissingField,
  BadType,             // JSON type of a field is wrong (incl. non-integral numbers)
  OutOfRange,          // value outside the documented range / enum / charset / length
};

struct ParseResult {
  ParseError err = ParseError::Ok;
  const char* field = nullptr;  // static string naming the offending field, or nullptr
  bool ok() const { return err == ParseError::Ok; }
};

// Parses and validates a "state" message. All-or-nothing: `out` is left
// untouched on any error. Unknown fields are ignored.
ParseResult parse_snapshot(const char* json, size_t len, Snapshot& out);

// Stable snake_case code for the wire: "ok","too_large","not_json","not_object",
// "wrong_type","unsupported_version","missing_field","bad_type","out_of_range".
const char* parse_error_code(ParseError e);

// Serialises a snapshot as a protocol "state" message (compact JSON). Returns the
// number of bytes written (excluding NUL), or 0 if `cap` is too small.
size_t snapshot_to_json(const Snapshot& s, char* buf, size_t cap);

}  // namespace bb
