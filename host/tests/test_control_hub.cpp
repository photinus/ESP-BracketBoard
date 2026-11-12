#include <cstring>
#include <string>
#include <vector>
#include "bb/control_hub.h"
#include "cJSON.h"
#include "doctest.h"

using namespace bb;

namespace {

const char* kTok = "s3cret-token";

std::string state_json(const char* session, uint32_t seq, int clock_ms = 60000) {
  Snapshot s;
  std::strcpy(s.session, session);
  s.seq = seq;
  s.left_score = 3;
  s.clock_ms_remaining = clock_ms;
  s.clock_state = ClockState::Running;
  char buf[1024];
  size_t n = snapshot_to_json(s, buf, sizeof buf);
  return std::string(buf, n);
}

std::string hello(const char* tok, int v = 1) {
  return std::string("{\"type\":\"hello\",\"v\":") + std::to_string(v) + ",\"token\":\"" + tok +
         "\",\"agent\":\"test/1\"}";
}

struct J {
  cJSON* j;
  explicit J(const std::string& s) : j(cJSON_Parse(s.c_str())) {}
  ~J() { cJSON_Delete(j); }
  J(const J&) = delete;
  const char* str(const char* k) const {
    cJSON* it = cJSON_GetObjectItemCaseSensitive(j, k);
    return cJSON_IsString(it) ? it->valuestring : nullptr;
  }
  bool has(const char* k) const { return cJSON_GetObjectItemCaseSensitive(j, k) != nullptr; }
  bool is_null(const char* k) const { return cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(j, k)); }
  double num(const char* k) const { return cJSON_GetObjectItemCaseSensitive(j, k)->valuedouble; }
  bool boolean(const char* k) const { return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, k)); }
};

struct StartedTime : FakeTimeSource {
  StartedTime() { t = 1000000; }
};

struct Fx {
  StartedTime ts;
  Board board;
  ControlHub hub;
  explicit Fx(bool manual = false, size_t max_pending = 3)
      : board(ts, cfg(manual)), hub(board, ts, kTok, 2000, max_pending) {}
  static BoardConfig cfg(bool manual) {
    BoardConfig c;
    c.manual_enabled = manual;
    return c;
  }
  std::vector<Outbound> text(ConnId c, const std::string& t) {
    std::vector<Outbound> out;
    hub.on_text(c, t.data(), t.size(), out);
    return out;
  }
  std::vector<Outbound> tick() {
    std::vector<Outbound> out;
    hub.tick(out);
    return out;
  }
  // open + authenticate
  void login(ConnId c) {
    hub.on_open(c);
    auto o = text(c, hello(kTok));
    REQUIRE(o.size() >= 1);
  }
};

}  // namespace

TEST_CASE("hub: good hello -> hello_ack") {
  Fx f;
  f.hub.on_open(1);
  auto out = f.text(1, hello(kTok));
  REQUIRE(out.size() == 1);
  CHECK(out[0].conn == 1);
  CHECK_FALSE(out[0].close_after);
  J a(out[0].text);
  CHECK(std::string(a.str("type")) == "hello_ack");
  CHECK(std::string(a.str("device")) == "scoreboard");
  CHECK(std::string(a.str("fw")) == "0.1.0");
  CHECK(std::string(a.str("mode")) == "agent");
  CHECK(a.is_null("session"));
  CHECK(a.is_null("last_seq"));
  cJSON* sv = cJSON_GetObjectItemCaseSensitive(a.j, "supported_v");
  REQUIRE(cJSON_IsArray(sv));
  CHECK(cJSON_GetArraySize(sv) == 1);
  CHECK(cJSON_GetArrayItem(sv, 0)->valueint == 1);
  REQUIRE(f.hub.control_client().has_value());
  CHECK(*f.hub.control_client() == 1);
}

TEST_CASE("hub: hello_ack reports session/last_seq and manual mode") {
  Fx f(true);
  f.login(1);
  f.text(1, state_json("abc", 41));
  f.hub.on_open(2);
  auto out = f.text(2, hello(kTok));
  // out[0] is the replaced error for conn 1, out[1] the ack
  REQUIRE(out.size() == 2);
  J a(out[1].text);
  CHECK(std::string(a.str("mode")) == "agent");  // agent live
  CHECK(std::string(a.str("session")) == "abc");
  CHECK(a.num("last_seq") == 41);
  f.ts.advance_ms(3500);
  f.hub.on_open(3);
  auto o3 = f.text(3, hello(kTok));
  J b(o3.back().text);
  CHECK(std::string(b.str("mode")) == "manual");
}

TEST_CASE("hub: bad token -> auth_failed + close") {
  Fx f;
  f.hub.on_open(1);
  auto out = f.text(1, hello("nope"));
  REQUIRE(out.size() == 1);
  CHECK(out[0].close_after);
  J e(out[0].text);
  CHECK(std::string(e.str("type")) == "error");
  CHECK(std::string(e.str("code")) == "auth_failed");
  CHECK_FALSE(f.hub.control_client().has_value());
  // token never leaked
  CHECK(out[0].text.find("nope") == std::string::npos);
  // connection is forgotten: further text is ignored
  CHECK(f.text(1, hello(kTok)).empty());
}

TEST_CASE("hub: missing token / wrong token type -> auth_failed") {
  Fx f;
  f.hub.on_open(1);
  auto out = f.text(1, "{\"type\":\"hello\",\"v\":1}");
  REQUIRE(out.size() == 1);
  CHECK(std::string(J(out[0].text).str("code")) == "auth_failed");
  f.hub.on_open(2);
  out = f.text(2, "{\"type\":\"hello\",\"v\":1,\"token\":5}");
  REQUIRE(out.size() == 1);
  CHECK(std::string(J(out[0].text).str("code")) == "auth_failed");
}

TEST_CASE("hub: wrong version -> unsupported_version + close") {
  Fx f;
  f.hub.on_open(1);
  auto out = f.text(1, hello(kTok, 2));
  REQUIRE(out.size() == 1);
  CHECK(out[0].close_after);
  CHECK(std::string(J(out[0].text).str("code")) == "unsupported_version");
  CHECK_FALSE(f.hub.control_client().has_value());
}

TEST_CASE("hub: unparsable or non-hello first frame -> bad_message + close") {
  Fx f;
  const char* bad[] = {"not json", "[1,2]", "{\"type\":\"ping\"}", "{\"v\":1,\"token\":\"x\"}", ""};
  ConnId id = 1;
  for (const char* b : bad) {
    f.hub.on_open(id);
    auto out = f.text(id, b);
    REQUIRE(out.size() == 1);
    CHECK(out[0].close_after);
    CHECK(std::string(J(out[0].text).str("code")) == "bad_message");
    ++id;
  }
}

TEST_CASE("hub: state before hello -> bad_message + close, not applied") {
  Fx f;
  f.hub.on_open(1);
  auto out = f.text(1, state_json("abc", 1));
  REQUIRE(out.size() == 1);
  CHECK(out[0].close_after);
  CHECK(std::string(J(out[0].text).str("code")) == "bad_message");
  CHECK_FALSE(f.board.has_agent_data());
}

TEST_CASE("hub: hello_timeout is strictly greater than 2000 ms") {
  Fx f;
  f.hub.on_open(1);
  f.ts.advance_ms(2000);
  CHECK(f.tick().empty());
  f.ts.advance_ms(1);
  auto out = f.tick();
  REQUIRE(out.size() == 1);
  CHECK(out[0].conn == 1);
  CHECK(out[0].close_after);
  CHECK(std::string(J(out[0].text).str("code")) == "hello_timeout");
  CHECK(f.tick().empty());  // removed
}

TEST_CASE("hub: authenticated client is not timed out") {
  Fx f;
  f.login(1);
  f.ts.advance_ms(60000);
  CHECK(f.tick().empty());
  REQUIRE(f.hub.control_client().has_value());
}

TEST_CASE("hub: second authenticated client replaces the first") {
  Fx f;
  f.login(1);
  f.hub.on_open(2);
  auto out = f.text(2, hello(kTok));
  REQUIRE(out.size() == 2);
  CHECK(out[0].conn == 1);
  CHECK(out[0].close_after);
  CHECK(std::string(J(out[0].text).str("code")) == "replaced");
  CHECK(out[1].conn == 2);
  CHECK(std::string(J(out[1].text).str("type")) == "hello_ack");
  CHECK(*f.hub.control_client() == 2);

  // state from the old client is ignored
  CHECK(f.text(1, state_json("abc", 1)).empty());
  CHECK_FALSE(f.board.has_agent_data());
  // old socket closing does not drop the new client
  f.hub.on_close(1);
  CHECK(*f.hub.control_client() == 2);
  auto ack = f.text(2, state_json("abc", 1));
  REQUIRE(ack.size() == 1);
  CHECK(std::string(J(ack[0].text).str("result")) == "applied");
}

TEST_CASE("hub: pending connection does not disturb the control client") {
  Fx f;
  f.login(1);
  f.hub.on_open(2);
  CHECK(*f.hub.control_client() == 1);
  // pending conn sends garbage and is dropped; client unaffected
  auto out = f.text(2, "garbage");
  REQUIRE(out.size() == 1);
  CHECK(out[0].conn == 2);
  CHECK(*f.hub.control_client() == 1);
  // pending conn timing out: only it is closed
  f.hub.on_open(3);
  f.ts.advance_ms(2001);
  out = f.tick();
  REQUIRE(out.size() == 1);
  CHECK(out[0].conn == 3);
  CHECK(*f.hub.control_client() == 1);
  // bad token from a pending conn does not replace
  f.hub.on_open(4);
  f.text(4, hello("bad"));
  CHECK(*f.hub.control_client() == 1);
}

TEST_CASE("hub: ack results applied / duplicate / stale / invalid") {
  Fx f;
  f.login(1);
  const std::string s5 = state_json("abc", 5);
  auto o = f.text(1, s5);
  REQUIRE(o.size() == 1);
  CHECK_FALSE(o[0].close_after);
  {
    J a(o[0].text);
    CHECK(std::string(a.str("type")) == "ack");
    CHECK(a.num("seq") == 5);
    CHECK(std::string(a.str("result")) == "applied");
    CHECK_FALSE(a.has("error"));
    CHECK_FALSE(a.has("field"));
  }
  o = f.text(1, s5);
  CHECK(std::string(J(o[0].text).str("result")) == "duplicate");
  o = f.text(1, state_json("abc", 4));
  {
    J a(o[0].text);
    CHECK(std::string(a.str("result")) == "stale");
    CHECK(a.num("seq") == 4);
  }
  std::string bad = state_json("abc", 6);
  size_t p = bad.find("\"left_score\":3");
  REQUIRE(p != std::string::npos);
  bad.replace(p, 14, "\"left_score\":100");
  o = f.text(1, bad);
  REQUIRE(o.size() == 1);
  {
    J a(o[0].text);
    CHECK(std::string(a.str("type")) == "ack");
    CHECK(std::string(a.str("result")) == "invalid");
    CHECK(std::string(a.str("error")) == "out_of_range");
    CHECK(std::string(a.str("field")) == "left_score");
  }
  // invalid without a field (wrong v)
  o = f.text(1, "{\"type\":\"state\",\"v\":2}");
  {
    J a(o[0].text);
    CHECK(std::string(a.str("result")) == "invalid");
    CHECK(std::string(a.str("error")) == "unsupported_version");
  }
  // oversize state frame
  std::string big = "{\"type\":\"state\",\"pad\":\"" + std::string(2000, 'x') + "\"}";
  o = f.text(1, big);
  {
    J a(o[0].text);
    CHECK(std::string(a.str("result")) == "invalid");
    CHECK(std::string(a.str("error")) == "too_large");
  }
  CHECK(f.hub.control_client().has_value());
}

TEST_CASE("hub: non-state message from client -> bad_message, stays connected") {
  Fx f;
  f.login(1);
  auto o = f.text(1, "{\"type\":\"ping\"}");
  REQUIRE(o.size() == 1);
  CHECK_FALSE(o[0].close_after);
  CHECK(std::string(J(o[0].text).str("code")) == "bad_message");
  o = f.text(1, "totally not json");
  REQUIRE(o.size() == 1);
  CHECK(std::string(J(o[0].text).str("code")) == "bad_message");
  CHECK_FALSE(o[0].close_after);
  CHECK(*f.hub.control_client() == 1);
  // still works afterwards
  o = f.text(1, state_json("abc", 1));
  CHECK(std::string(J(o[0].text).str("result")) == "applied");
}

TEST_CASE("hub: client close frees the slot") {
  Fx f;
  f.login(1);
  f.hub.on_close(1);
  CHECK_FALSE(f.hub.control_client().has_value());
  f.hub.on_open(2);
  f.hub.on_close(2);
  f.ts.advance_ms(5000);
  CHECK(f.tick().empty());  // closed pending conn is gone
}

TEST_CASE("hub: max_pending enforced; excess connections are ignored") {
  Fx f(false, 3);
  f.hub.on_open(1);
  f.hub.on_open(2);
  f.hub.on_open(3);
  f.hub.on_open(4);  // over the cap: untracked
  CHECK(f.text(4, hello(kTok)).empty());
  CHECK_FALSE(f.hub.control_client().has_value());
  // after one pending conn resolves, a new one is accepted
  f.hub.on_close(1);
  f.hub.on_open(5);
  auto out = f.text(5, hello(kTok));
  REQUIRE(out.size() == 1);
  CHECK(*f.hub.control_client() == 5);
  // authenticated connection no longer counts as pending
  f.hub.on_open(6);
  CHECK(f.text(6, hello("x")).size() == 1);
  // timeouts only report tracked conns (2,3 left pending)
  f.ts.advance_ms(2001);
  out = f.tick();
  CHECK(out.size() == 2);
}

TEST_CASE("hub: text for unknown connection is ignored") {
  Fx f;
  CHECK(f.text(99, hello(kTok)).empty());
  CHECK_FALSE(f.hub.control_client().has_value());
}

TEST_CASE("hub: agent restart (new session seq 1) is applied") {
  Fx f;
  f.login(1);
  f.text(1, state_json("old", 900));
  auto o = f.text(1, state_json("new", 1));
  CHECK(std::string(J(o[0].text).str("result")) == "applied");
  CHECK(std::string(f.board.seq().session()) == "new");
  o = f.text(1, state_json("old", 901));  // old process now looks like a new session too
  CHECK(std::string(J(o[0].text).str("result")) == "applied");
}

TEST_CASE("hub: WS and HTTP share the seq filter") {
  Fx f;
  f.login(1);
  const std::string s = state_json("abc", 10);
  auto hdr = std::string("Bearer ") + kTok;
  auto r = f.hub.on_http_state(hdr.c_str(), s.data(), s.size());
  CHECK(r.status == 200);
  auto o = f.text(1, s);
  CHECK(std::string(J(o[0].text).str("result")) == "duplicate");
}

TEST_CASE("hub: HTTP /state auth variants") {
  Fx f;
  const std::string s = state_json("abc", 1);
  CHECK(f.hub.on_http_state(nullptr, s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("Bearer wrong", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("Bearer", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("Bearer ", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("Basic s3cret-token", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("s3cret-token", s.data(), s.size()).status == 401);
  CHECK(f.hub.on_http_state("Bearer s3cret-token-x", s.data(), s.size()).status == 401);
  CHECK_FALSE(f.board.has_agent_data());
  auto r = f.hub.on_http_state("Bearer s3cret-token", s.data(), s.size());
  CHECK(r.status == 200);
  CHECK(std::string(J(r.body).str("result")) == "applied");
  // case-insensitive scheme
  std::string s2 = state_json("abc", 2);
  CHECK(f.hub.on_http_state("bEaReR s3cret-token", s2.data(), s2.size()).status == 200);
  // 401 bodies never include the token
  auto u = f.hub.on_http_state("Bearer wrong", s.data(), s.size());
  CHECK(u.body.find("wrong") == std::string::npos);
  CHECK(u.body.find(kTok) == std::string::npos);
}

TEST_CASE("hub: HTTP /state 200/400/413") {
  Fx f;
  const std::string hdr = std::string("Bearer ") + kTok;
  const std::string s = state_json("abc", 3);
  CHECK(f.hub.on_http_state(hdr.c_str(), s.data(), s.size()).status == 200);
  auto d = f.hub.on_http_state(hdr.c_str(), s.data(), s.size());
  CHECK(d.status == 200);
  CHECK(std::string(J(d.body).str("result")) == "duplicate");
  auto st = state_json("abc", 2);
  auto stale = f.hub.on_http_state(hdr.c_str(), st.data(), st.size());
  CHECK(stale.status == 200);
  CHECK(std::string(J(stale.body).str("result")) == "stale");

  std::string inv = "{\"type\":\"state\",\"v\":1}";
  auto r = f.hub.on_http_state(hdr.c_str(), inv.data(), inv.size());
  CHECK(r.status == 400);
  J a(r.body);
  CHECK(std::string(a.str("result")) == "invalid");
  CHECK(std::string(a.str("error")) == "missing_field");
  CHECK(a.has("field"));

  std::string big(1025, ' ');
  r = f.hub.on_http_state(hdr.c_str(), big.data(), big.size());
  CHECK(r.status == 413);
  CHECK(std::string(J(r.body).str("error")) == "too_large");
  std::string ok1024 = "{\"type\":\"state\"" + std::string(1024 - 15 - 1, ' ') + "}";
  REQUIRE(ok1024.size() == 1024);
  r = f.hub.on_http_state(hdr.c_str(), ok1024.data(), ok1024.size());
  CHECK(r.status == 400);  // within size limit, just invalid
  // 401 takes precedence over 413
  CHECK(f.hub.on_http_state("Bearer bad", big.data(), big.size()).status == 401);
}

TEST_CASE("hub: /manual/cmd 401 / 409 / 400 / 200") {
  Fx f(true);
  const std::string hdr = std::string("Bearer ") + kTok;
  const std::string cmd = "{\"cmd\":\"score\",\"side\":\"left\",\"delta\":2}";
  CHECK(f.hub.on_http_manual_cmd(nullptr, cmd.data(), cmd.size()).status == 401);
  CHECK(f.hub.on_http_manual_cmd("Bearer no", cmd.data(), cmd.size()).status == 401);

  auto r = f.hub.on_http_manual_cmd(hdr.c_str(), cmd.data(), cmd.size());
  CHECK(r.status == 200);
  CHECK(std::string(J(r.body).str("result")) == "applied");
  CHECK(f.board.frame().left_score == 2);

  r = f.hub.on_http_manual_cmd(hdr.c_str(), "{\"cmd\":\"nope\"}", 14);
  CHECK(r.status == 400);
  CHECK(std::string(J(r.body).str("result")) == "invalid");
  r = f.hub.on_http_manual_cmd(hdr.c_str(), "garbage", 7);
  CHECK(r.status == 400);

  // agent live -> locked
  const std::string s = state_json("abc", 1);
  f.hub.on_http_state(hdr.c_str(), s.data(), s.size());
  r = f.hub.on_http_manual_cmd(hdr.c_str(), cmd.data(), cmd.size());
  CHECK(r.status == 409);
  CHECK(std::string(J(r.body).str("result")) == "manual_locked");
  // agent stale -> unlocked again, manual state was kept
  f.ts.advance_ms(3001);
  r = f.hub.on_http_manual_cmd(hdr.c_str(), cmd.data(), cmd.size());
  CHECK(r.status == 200);
  CHECK(f.board.frame().left_score == 4);
}

TEST_CASE("hub: /manual/cmd locked when manual disabled") {
  Fx f(false);
  const std::string hdr = std::string("Bearer ") + kTok;
  auto r = f.hub.on_http_manual_cmd(hdr.c_str(), "{\"cmd\":\"swap\"}", 14);
  CHECK(r.status == 409);
}

TEST_CASE("hub: status_json") {
  Fx f(true);
  {
    J s(f.hub.status_json());
    CHECK(std::string(s.str("device")) == "scoreboard");
    CHECK(std::string(s.str("fw")) == "0.1.0");
    CHECK(std::string(s.str("mode")) == "manual");
    CHECK_FALSE(s.boolean("agent_live"));
    CHECK(s.is_null("session"));
    CHECK(s.is_null("last_seq"));
    CHECK(s.num("uptime_s") == 0);
  }
  f.ts.advance_ms(5500);
  f.login(1);
  f.text(1, state_json("abc", 77));
  std::string js = f.hub.status_json();
  CHECK(js.find(kTok) == std::string::npos);
  CHECK(js.find("token") == std::string::npos);
  J s(js);
  CHECK(std::string(s.str("mode")) == "agent");
  CHECK(s.boolean("agent_live"));
  CHECK(std::string(s.str("session")) == "abc");
  CHECK(s.num("last_seq") == 77);
  CHECK(s.num("uptime_s") == 5);
  f.ts.advance_ms(3001);
  J s2(f.hub.status_json());
  CHECK(std::string(s2.str("mode")) == "manual");
  CHECK_FALSE(s2.boolean("agent_live"));
  CHECK(s2.num("uptime_s") == 8);
}

TEST_CASE("hub: status mode agent when manual disabled and no data") {
  Fx f(false);
  J s(f.hub.status_json());
  CHECK(std::string(s.str("mode")) == "agent");
}
