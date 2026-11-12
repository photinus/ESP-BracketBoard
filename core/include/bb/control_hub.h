// Transport-agnostic control protocol state machine (WS hello/auth/single client + HTTP).
// The firmware maps sockets to ConnId and ships Outbound frames; the host CLI does the same
// over stdio. NOT thread-safe. See docs/protocol-v1.md section 3.
#pragma once
#include <cstddef>
#include <optional>
#include <string>
#include <vector>
#include "bb/board.h"
#include "bb/time_source.h"

namespace bb {

using ConnId = int;

struct Outbound {
  ConnId conn;
  std::string text;
  bool close_after = false;
};

struct HttpReply {
  int status = 500;
  std::string body;  // JSON
};

struct HubInfo {            // used to fill hello_ack / status
  std::string device = "scoreboard";
  std::string fw = "0.1.0";
};

class ControlHub {
 public:
  ControlHub(Board& board, const TimeSource& ts, std::string token,
             int hello_timeout_ms = 2000, size_t max_pending = 3, HubInfo info = {});

  // Adds a pending connection. Returns false if rejected (max_pending already pending): the
  // transport MUST then close that socket (the hub ignores every frame from it).
  bool on_open(ConnId c);
  // Handles one text frame. Appends zero or more Outbound to `out`.
  //  - pending conn: first frame must be a valid hello with correct token -> hello_ack, becomes
  //    the control client (previous client gets {"type":"error","code":"replaced"} + close).
  //    Bad token -> error auth_failed + close. Non-hello / unparsable / wrong version ->
  //    error bad_message / unsupported_version + close.
  //  - control client: {"type":"state",...} -> Board::ingest_state(Ws) -> ack.
  //    Other message types -> error bad_message (connection stays open).
  //  - any other conn: ignored.
  void on_text(ConnId c, const char* data, size_t len, std::vector<Outbound>& out);
  void on_close(ConnId c);
  // Emits hello_timeout + close for pending conns older than hello_timeout_ms.
  void tick(std::vector<Outbound>& out);

  // POST /state. `authorization_hdr` is the raw header value ("Bearer <token>") or nullptr.
  //  401 bad/missing token; 413 body too large; 400 invalid (ack body with error+field);
  //  200 applied/duplicate/stale (ack body).
  HttpReply on_http_state(const char* authorization_hdr, const char* body, size_t len);

  // POST /manual/cmd. Same auth. 409 {"result":"manual_locked"} unless Board::manual_active();
  // 400 on malformed command; 200 {"result":"applied"} otherwise.
  HttpReply on_http_manual_cmd(const char* authorization_hdr, const char* body, size_t len);

  // GET /status JSON (no auth, never contains the token):
  // {"device","fw","mode":"agent"|"manual","agent_live","session","last_seq","uptime_s"}
  std::string status_json() const;

  std::optional<ConnId> control_client() const { return client_; }

 private:
  struct Pending { ConnId id; int64_t opened_us; };

  Board& board_;
  const TimeSource& ts_;
  std::string token_;
  int hello_timeout_ms_;
  size_t max_pending_;
  HubInfo info_;
  int64_t boot_us_;
  std::vector<Pending> pending_;
  std::optional<ConnId> client_;
};

}  // namespace bb
