"""The `openai-wif-proxy` composite's forwarder
(.github/actions/openai-wif-proxy/openai_wif_proxy.py; design/untrusted-agent-
job.md → Codex jobs: OpenAI API Platform workload identity federation, and →
Testing), run as the composite runs it, against local stub servers for
GitHub's OIDC endpoint, OpenAI's token endpoint and the upstream:

- the first exchange sends exactly the five token-exchange fields, the
  subject token requested for the configured audience; a failed first
  exchange fails start-up with the exchange's error and no token, and
  starts no forwarder;
- renewal happens once the token is within 60 seconds of expiry, with a
  fresh subject token each time (the stub's tokens live 62 seconds, so the
  real margin is exercised in seconds);
- an upstream 401 triggers exactly one re-exchange and one retry of the
  buffered body; a failed renewal answers 502 with the fixed message, and
  so does an upstream redirect, which is never followed;
- only `POST /v1/responses` with no query is forwarded: another method, path,
  query or absolute-form target, a chunked or length-less body and a body
  over the cap are refused before anything reaches the upstream;
- the incoming Authorization (codex-action's placeholder) is replaced, never
  forwarded;
- a server-sent-events response is relayed chunk by chunk, a body with a
  length byte for byte;
- the token appears in no file under the runner's temp or home, in no log
  line and in no response to a client: only in the `::add-mask::` line;
- `stop` reports the counts and stops the forwarder; `probe` records an
  exchange, a refusal or an outage against its expectation;
- the composite's defaults are the identifiers set up on 2026-10-01, and
  its step runs with the system PATH.
"""

import base64
import http.client
import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github" / "actions" / "openai-wif-proxy"
SCRIPT = ACTION / "openai_wif_proxy.py"
REQ_TOKEN = "REQ-SECRET-oidc-request-token"
AUDIENCE = "openai-wif:meridianlabs-ai"
PLACEHOLDER = "meridian-wif-placeholder"
RENEWAL_FAILED = b'{"error":{"message":"openai-wif-proxy: token renewal failed","type":"proxy_error"}}'
REDIRECT_REFUSED = b'{"error":{"message":"openai-wif-proxy: upstream redirect refused","type":"proxy_error"}}'


def b64(doc: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(doc).encode()).decode().rstrip("=")


class Stub:
    """GitHub's OIDC endpoint (/oidc), OpenAI's token endpoint (/token) and
    the Responses API (/v1/responses), recording every call."""

    def __init__(self):
        self.lock = threading.Lock()
        self.oidc, self.exchanges, self.upstream = [], [], []
        self.expires_in = 3600
        self.exchange_plan = []          # per call: None (grant), or (status, body)
        self.upstream_plan = []          # per call: callable(handler) or None (200 JSON echo)
        self.release = threading.Event()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, status, body=b"", headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(url.query)
                with stub.lock:
                    n = len(stub.oidc) + 1
                    stub.oidc.append({"auth": self.headers.get("Authorization"), "query": q})
                now = int(time.time())
                jwt = f"{b64({'alg': 'RS256'})}.{b64({'aud': q['audience'][0], 'iat': now, 'exp': now + 300, 'event_name': 'issue_comment', 'job_workflow_ref': 'meridianlabs-ai/agents/.github/workflows/claude-review.yml@refs/heads/main', 'n': n})}.SIG-SECRET-{n}"
                self.reply(200, json.dumps({"value": jwt}).encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/token":
                    with stub.lock:
                        n = len(stub.exchanges) + 1
                        stub.exchanges.append({"form": urllib.parse.parse_qs(body.decode(), keep_blank_values=True),
                                               "raw": body.decode(), "ctype": self.headers.get("Content-Type")})
                        plan = stub.exchange_plan.pop(0) if stub.exchange_plan else None
                    if plan:
                        return self.reply(plan[0], plan[1].encode())
                    return self.reply(200, json.dumps({"access_token": f"OAT-SECRET-{n}", "token_type": "Bearer",
                                                       "expires_in": stub.expires_in,
                                                       "expires_at": int(time.time()) + stub.expires_in}).encode())
                with stub.lock:
                    stub.upstream.append({"path": self.path, "headers": dict(self.headers.items()), "body": body})
                    plan = stub.upstream_plan.pop(0) if stub.upstream_plan else None
                if plan:
                    return plan(self)
                self.reply(200, json.dumps({"ok": True, "n": len(stub.upstream)}).encode(),
                           [("Content-Type", "application/json"), ("X-Upstream", "1")])

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


class Run:
    def __init__(self, tmp_path, stub):
        self.tmp, self.stub = tmp_path, stub
        self.temp = tmp_path / "runner-temp"
        self.home = tmp_path / "home"
        self.temp.mkdir()
        self.home.mkdir()
        self.state = self.temp / "openai-wif"
        self.output = self.temp / "output"
        self.summary = self.temp / "summary"
        self.outputs = []

    def env(self, oidc=True):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "RUNNER_TEMP": str(self.temp),
               "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary)}
        if oidc:
            env |= {"ACTIONS_ID_TOKEN_REQUEST_URL": f"{self.stub.base}/oidc?api-version=2.0",
                    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQ_TOKEN}
        return env

    def script(self, *args, oidc=True):
        r = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env(oidc), capture_output=True,
                           text=True, timeout=60)
        self.outputs.append(r.stdout + r.stderr)
        return r

    def ids(self, audience=AUDIENCE):
        return ["--identity-provider-id", "idp_test", "--service-account-id", "svc_test", "--audience", audience,
                "--token-url", f"{self.stub.base}/token"]

    def start(self, **kw):
        r = self.script("serve", "--state-dir", str(self.state), *self.ids(),
                        "--upstream-url", f"{self.stub.base}/v1/responses", **kw)
        return r

    @property
    def endpoint(self):
        m = re.search(r"^endpoint=(.+)$", self.output.read_text(), re.M)
        return m.group(1)

    @property
    def port(self):
        return int(urllib.parse.urlsplit(self.endpoint).port)

    def pid(self):
        p = self.state / "pid"
        return int(p.read_text()) if p.exists() else None

    def post(self, body=b'{"model":"m"}', path="/v1/responses", headers=None, method="POST"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Authorization": f"Bearer {PLACEHOLDER}", "Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        self.outputs.append(data.decode("latin-1"))
        return resp, data

    def log(self):
        return (self.state / "log").read_text() if (self.state / "log").exists() else ""

    def kill(self):
        pid = self.pid()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def assert_no_secret_leaked(self):
        """No issued token, subject token or request token in any file under
        the runner's temp or home, in the forwarder's log, or in anything a
        client got; on the script's output only in mask commands."""
        secrets = [REQ_TOKEN, "SIG-SECRET-"] + [f"OAT-SECRET-{n + 1}" for n in range(len(self.stub.exchanges))]
        files = [p for root in (self.temp, self.home) for p in root.rglob("*") if p.is_file()]
        for p in files:
            text = p.read_text(errors="replace")
            for s in secrets:
                assert s not in text, (p, s)
        for out in self.outputs:
            for line in out.splitlines():
                if any(s in line for s in secrets):
                    assert line.startswith("::add-mask::"), line


@pytest.fixture
def run(tmp_path, stub):
    r = Run(tmp_path, stub)
    yield r
    r.kill()


# --- start-up ----------------------------------------------------------------


def test_the_first_exchange_sends_exactly_the_token_exchange_fields(run, stub):
    r = run.start()
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(stub.oidc) == 1 and len(stub.exchanges) == 1
    assert stub.oidc[0]["auth"] == f"bearer {REQ_TOKEN}"
    assert stub.oidc[0]["query"] == {"api-version": ["2.0"], "audience": [AUDIENCE]}
    form = stub.exchanges[0]["form"]
    assert set(form) == {"grant_type", "identity_provider_id", "service_account_id", "subject_token_type",
                         "subject_token"}
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:token-exchange"]
    assert form["subject_token_type"] == ["urn:ietf:params:oauth:token-type:jwt"]
    assert form["identity_provider_id"] == ["idp_test"] and form["service_account_id"] == ["svc_test"]
    jwt = form["subject_token"][0]
    assert jwt.endswith(".SIG-SECRET-1")
    payload = jwt.split(".")[1]
    assert json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["aud"] == AUDIENCE
    assert stub.exchanges[0]["ctype"] == "application/x-www-form-urlencoded"
    # It listens on loopback, and says what it measured.
    assert urllib.parse.urlsplit(run.endpoint).hostname == "127.0.0.1"
    assert urllib.parse.urlsplit(run.endpoint).path == "/v1/responses"
    assert "token expires_in 3600 s, GitHub OIDC token lifetime (exp - iat) 300 s" in run.summary.read_text()
    assert oct(run.state.stat().st_mode & 0o777) == "0o700"
    assert oct((run.state / "log").stat().st_mode & 0o777) == "0o600"
    # The parent returned; the forwarder serves on.
    resp, _ = run.post()
    assert resp.status == 200
    run.assert_no_secret_leaked()


@pytest.mark.parametrize("plan, message", [
    ((401, '{"error":"invalid_grant","error_description":"no mapping matched"}'),
     "the OpenAI token exchange answered HTTP 401: invalid_grant no mapping matched"),
    ((400, '{"error":{"code":"invalid_request","message":"bad service account"}}'),
     "the OpenAI token exchange answered HTTP 400: invalid_request bad service account"),
    ((200, '{"token_type":"Bearer"}'), "the OpenAI token exchange answered HTTP 200 without a usable access_token"),
    ((200, '{"access_token":"OAT-SECRET-1"}'), "the OpenAI token exchange answered HTTP 200 without a usable expiry"),
    ((500, "oops"), "the OpenAI token exchange answered HTTP 500"),
])
def test_a_failed_first_exchange_fails_start_up_without_a_token(run, stub, plan, message):
    stub.exchange_plan = [plan] * 3
    r = run.start()
    assert r.returncode == 1
    assert f"::error::openai-wif-proxy: {message}" in r.stdout, r.stdout
    assert run.pid() is None and not run.output.exists()
    run.assert_no_secret_leaked()


def test_a_transient_exchange_failure_is_retried(run, stub):
    stub.exchange_plan = [(503, "busy")]
    r = run.start()
    assert r.returncode == 0, r.stdout
    assert len(stub.exchanges) == 2


def test_no_oidc_request_token_fails_start_up(run, stub):
    r = run.start(oidc=False)
    assert r.returncode == 1 and "no OIDC request token in this job" in r.stdout
    assert stub.exchanges == []


def test_a_non_loopback_plain_http_url_is_refused(run, stub):
    r = run.script("serve", "--state-dir", str(run.state), *run.ids(), "--upstream-url", "http://example.com/v1/responses")
    assert r.returncode == 1 and "refusing URL scheme or host: http://example.com" in r.stdout
    assert stub.oidc == []


# --- renewal -----------------------------------------------------------------


def test_renewal_comes_before_expiry_with_a_fresh_subject_token(run, stub):
    stub.expires_in = 62                     # fresh for 2 s under the 60 s margin
    assert run.start().returncode == 0
    for pause in (0, 2.5, 0, 2.5):
        time.sleep(pause)
        resp, _ = run.post()
        assert resp.status == 200
    assert len(stub.exchanges) == 3
    subjects = [e["form"]["subject_token"][0] for e in stub.exchanges]
    assert len(set(subjects)) == 3 and len(stub.oidc) == 3
    assert all(o["query"]["audience"] == [AUDIENCE] for o in stub.oidc)
    used = [u["headers"]["Authorization"] for u in stub.upstream]
    assert used == ["Bearer OAT-SECRET-1", "Bearer OAT-SECRET-2", "Bearer OAT-SECRET-2", "Bearer OAT-SECRET-3"]
    assert run.log().count("renewed (expiry): expires_in=62") == 2
    run.assert_no_secret_leaked()


def test_an_upstream_401_re_exchanges_once_and_retries_the_body(run, stub):
    assert run.start().returncode == 0
    stub.upstream_plan = [lambda h: h.reply(401, b'{"error":"expired"}')]
    resp, data = run.post(body=b'{"input":"exactly these bytes"}')
    assert resp.status == 200 and json.loads(data)["ok"] is True
    assert len(stub.exchanges) == 2 and len(stub.upstream) == 2
    assert [u["body"] for u in stub.upstream] == [b'{"input":"exactly these bytes"}'] * 2
    assert [u["headers"]["Authorization"] for u in stub.upstream] == ["Bearer OAT-SECRET-1", "Bearer OAT-SECRET-2"]
    # A second 401 is OpenAI's answer: relayed, no third try.
    stub.upstream_plan = [lambda h: h.reply(401, b'{"error":"no"}')] * 2
    resp, data = run.post()
    assert resp.status == 401 and data == b'{"error":"no"}'
    assert len(stub.exchanges) == 3 and len(stub.upstream) == 4
    assert "renewed (upstream 401)" in run.log()
    run.assert_no_secret_leaked()


def test_a_failed_renewal_answers_502_with_the_fixed_message(run, stub):
    stub.expires_in = 61
    assert run.start().returncode == 0
    stub.exchange_plan = [(401, '{"error":"invalid_grant"}')]
    time.sleep(1.2)
    resp, data = run.post()
    assert resp.status == 502 and data == RENEWAL_FAILED
    assert stub.upstream == []
    assert "renewal failed (expiry): the OpenAI token exchange answered HTTP 401: invalid_grant" in run.log()
    # The next request tries again.
    resp, _ = run.post()
    assert resp.status == 200 and len(stub.exchanges) == 3
    run.assert_no_secret_leaked()


def test_an_upstream_redirect_is_never_followed(run, stub):
    assert run.start().returncode == 0
    stub.upstream_plan = [lambda h: h.reply(302, b"", [("Location", "https://elsewhere.example/v1/responses")])]
    resp, data = run.post()
    assert resp.status == 502 and data == REDIRECT_REFUSED
    assert len(stub.upstream) == 1
    assert "upstream error: redirect (HTTP 302) not followed" in run.log()


# --- what is forwarded -------------------------------------------------------


def raw(port, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


@pytest.mark.parametrize("request_bytes, status", [
    (b"GET /v1/responses HTTP/1.1\r\nHost: x\r\n\r\n", 403),
    (b"PUT /v1/responses HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 403),
    (b"POST /v1/models HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 403),
    (b"POST /v1/responses?stream=true HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 403),
    (b"POST /v1/responses/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 403),
    (b"POST http://api.openai.com/v1/responses HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n", 403),
    (b"POST /v1/responses HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 411),
    (b"POST /v1/responses HTTP/1.1\r\nHost: x\r\n\r\n", 411),
    (b"POST /v1/responses HTTP/1.1\r\nHost: x\r\nContent-Length: 33554433\r\n\r\n", 413),
])
def test_only_post_v1_responses_is_forwarded(run, stub, request_bytes, status):
    assert run.start().returncode == 0
    out = raw(run.port, request_bytes)
    assert out.startswith(f"HTTP/1.1 {status} ".encode()), out[:80]
    assert stub.upstream == []
    assert "refused: " in run.log()


def test_a_body_at_the_cap_is_forwarded(run, stub):
    assert run.start().returncode == 0
    body = b"x" * (32 * 1024 * 1024)
    resp, _ = run.post(body=body)
    assert resp.status == 200 and stub.upstream[0]["body"] == body


def test_the_incoming_authorization_is_replaced(run, stub):
    assert run.start().returncode == 0
    resp, _ = run.post(headers={"OpenAI-Beta": "responses=v1", "Accept": "text/event-stream",
                                   "Proxy-Authorization": "Basic nope"})
    assert resp.status == 200 and resp.getheader("X-Upstream") == "1"
    seen = stub.upstream[0]["headers"]
    assert seen["Authorization"] == "Bearer OAT-SECRET-1"
    assert PLACEHOLDER not in json.dumps(seen)
    assert "Proxy-Authorization" not in seen
    assert seen["OpenAI-Beta"] == "responses=v1" and seen["Accept"] == "text/event-stream"
    assert seen["Host"] == f"127.0.0.1:{stub.port}"
    assert stub.upstream[0]["path"] == "/v1/responses"
    run.assert_no_secret_leaked()


def test_a_server_sent_events_stream_is_relayed_chunk_by_chunk(run, stub):
    assert run.start().returncode == 0

    def sse(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        for event in (b"data: one\n\n",):
            h.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
            h.wfile.flush()
        stub.release.wait(20)
        event = b"data: two\n\n"
        h.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(event), event))
        h.wfile.flush()

    stub.upstream_plan = [sse]
    conn = http.client.HTTPConnection("127.0.0.1", run.port, timeout=10)
    conn.request("POST", "/v1/responses", body=b"{}", headers={"Authorization": f"Bearer {PLACEHOLDER}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    first = resp.read1(65536)
    # The first event arrived while the upstream still holds the second.
    assert first == b"data: one\n\n" and not stub.release.is_set()
    stub.release.set()
    assert resp.read() == b"data: two\n\n"
    conn.close()


def test_a_body_with_a_length_is_relayed_byte_for_byte(run, stub):
    assert run.start().returncode == 0
    blob = bytes(range(256)) * 300
    stub.upstream_plan = [lambda h: h.reply(200, blob, [("Content-Encoding", "gzip")])]
    resp, data = run.post()
    assert resp.status == 200 and data == blob
    assert resp.getheader("Content-Encoding") == "gzip" and resp.getheader("Content-Length") == str(len(blob))


# --- stop and probe ----------------------------------------------------------


def test_stop_reports_the_counts_and_stops_the_forwarder(run, stub):
    stub.expires_in = 62
    assert run.start().returncode == 0
    run.post()
    raw(run.port, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    time.sleep(2.2)
    run.post()
    pid = run.pid()
    r = run.script("stop", "--state-dir", str(run.state))
    assert r.returncode == 0
    line = ("openai-wif-proxy: 2 requests forwarded, 1 renewals before expiry, 0 after an upstream 401, "
            "0 failed renewals, 0 upstream errors, 1 refused requests")
    assert line in r.stdout and line in run.summary.read_text()
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the forwarder is still running")
    run.assert_no_secret_leaked()


def probe(run, expect, audience=AUDIENCE, oidc=True):
    return run.script("probe", "--expect", expect, *run.ids(audience), oidc=oidc)


def test_probe_records_a_refusal(run, stub):
    stub.exchange_plan = [(401, '{"error":"invalid_grant","error_description":"no mapping matched"}')] * 2
    r = probe(run, "refused", audience="openai-wif:wrong")
    assert r.returncode == 0, r.stdout
    assert ("OpenAI token exchange (aud=openai-wif:wrong, event_name=issue_comment, job_workflow_ref="
            "meridianlabs-ai/agents/.github/workflows/claude-review.yml@refs/heads/main): refused (the OpenAI "
            "token exchange answered HTTP 401: invalid_grant no mapping matched); expected refused") in r.stdout
    assert stub.oidc[0]["query"]["audience"] == ["openai-wif:wrong"]
    r = probe(run, "exchanged")
    assert r.returncode == 1 and "outcome is refused, expected exchanged" in r.stdout
    run.assert_no_secret_leaked()


def test_probe_fails_when_the_exchange_grants_a_token(run, stub):
    r = probe(run, "refused")
    assert r.returncode == 1 and ": exchanged (HTTP 2xx, a token (dropped)); expected refused" in r.stdout
    assert probe(run, "exchanged").returncode == 0
    run.assert_no_secret_leaked()


@pytest.mark.parametrize("oidc, plan", [(False, None), (True, (500, "down"))])
def test_probe_counts_an_outage_as_neither(run, stub, oidc, plan):
    stub.exchange_plan = [plan] * 9 if plan else []  # three probes, three attempts each
    for expect in ("refused", "exchanged"):
        r = probe(run, expect, oidc=oidc)
        assert r.returncode == 1 and ": error (" in r.stdout, r.stdout
    assert probe(run, "any", oidc=oidc).returncode == 0


# --- the composite -----------------------------------------------------------


def test_the_composite_defaults_are_the_configured_identifiers():
    text = (ACTION / "action.yml").read_text()
    inputs = text[text.index("\ninputs:\n"):text.index("\noutputs:\n")]
    defaults = dict(re.findall(r"^  ([a-z-]+):\n(?:    .*\n|      .*\n)*?    default: (.*)$", inputs, re.M))
    assert defaults == {"mode": "start", "identity-provider-id": "idp_1158bc670035bcb6c1f79de4",
                        "service-account-id": "user-4vdBIc98XKYif77VbHOkmScs",
                        "audience": "openai-wif:meridianlabs-ai", "expect": '""'}
    run_block = text[text.index("      run: |\n"):]
    assert "        PATH=/usr/sbin:/usr/bin:/sbin:/bin\n" in run_block
    assert run_block.index("PATH=/usr/sbin") < run_block.index("python3")
    assert '        state="$RUNNER_TEMP/openai-wif"\n' in run_block
    assert "post:" not in text and "post-if" not in text


def test_the_composite_step_starts_and_stops_the_forwarder(tmp_path, stub):
    # The composite's own step, lifted, with the script's test URLs injected
    # through a wrapper (the composite passes none).
    text = (ACTION / "action.yml").read_text()
    body = text[text.index("      run: |\n") + len("      run: |\n"):]
    body = "\n".join(line[8:] for line in body.splitlines())
    wrapper = tmp_path / "wrap.py"
    wrapper.write_text(
        "import sys, runpy\n"
        f"extra = {{'serve': ['--token-url', '{stub.base}/token', '--upstream-url', '{stub.base}/v1/responses'],"
        f" 'probe': ['--token-url', '{stub.base}/token'], 'stop': []}}\n"
        "sys.argv = [sys.argv[1]] + sys.argv[2:] + extra[sys.argv[2]]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    body = body.replace('python3 "$SCRIPT"', f'{sys.executable} {wrapper} "$SCRIPT"')
    temp = tmp_path / "temp"
    temp.mkdir()
    env = {"RUNNER_TEMP": str(temp), "GITHUB_OUTPUT": str(temp / "out"), "GITHUB_STEP_SUMMARY": str(temp / "sum"),
           "IDP": "idp_1158bc670035bcb6c1f79de4", "SVC": "user-4vdBIc98XKYif77VbHOkmScs", "AUDIENCE": AUDIENCE,
           "SCRIPT": str(SCRIPT), "EXPECT": "", "HOME": str(tmp_path),
           "ACTIONS_ID_TOKEN_REQUEST_URL": f"{stub.base}/oidc?api-version=2.0",
           "ACTIONS_ID_TOKEN_REQUEST_TOKEN": REQ_TOKEN}
    try:
        r = subprocess.run(["bash", "-c", body], env=env | {"MODE": "start"}, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        assert stub.exchanges[0]["form"]["service_account_id"] == ["user-4vdBIc98XKYif77VbHOkmScs"]
        assert stub.exchanges[0]["form"]["identity_provider_id"] == ["idp_1158bc670035bcb6c1f79de4"]
        assert re.search(r"^endpoint=http://127\.0\.0\.1:\d+/v1/responses$", (temp / "out").read_text(), re.M)
        r = subprocess.run(["bash", "-c", body], env=env | {"MODE": "stop"}, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0 and "0 requests forwarded" in r.stdout
        r = subprocess.run(["bash", "-c", body], env=env | {"MODE": "bogus"}, capture_output=True, text=True)
        assert r.returncode == 1 and "mode must be start, stop or probe" in r.stdout
    finally:
        pid_file = temp / "openai-wif" / "pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except OSError:
                pass


# --- the negative canaries ---------------------------------------------------

WORKFLOWS = ROOT / ".github" / "workflows"
PROBE_STEP = ("        uses: meridianlabs-ai/agents/.github/actions/openai-wif-proxy@main\n"
              "        with:\n          mode: probe\n          expect: refused\n")
OIDC_ONLY = "    permissions:\n      contents: read\n      id-token: write\n"


def test_the_canaries_each_expect_a_refusal_outside_the_four_workflows():
    canary = (WORKFLOWS / "openai-wif-canary.yml").read_text()
    other = (WORKFLOWS / "openai-wif-canary-reusable.yml").read_text()
    # On workflow_run from main (an event the mapping lists, the workflow at
    # refs/heads/main), so only the workflow condition fails; by hand too.
    on = canary[canary.index("\non:\n"):canary.index("\npermissions:\n")]
    assert on == ('\non:\n  workflow_run:\n    workflows: ["engine isolation canary"]\n    types: [completed]\n'
                  "    branches: [main]\n  workflow_dispatch:\n")
    body = canary[canary.index("\njobs:\n"):]
    assert re.findall(r"^  ([a-z-]+):$", body, re.M) == ["no-reusable-workflow", "other-reusable-workflow"]
    job = body[body.index("  no-reusable-workflow:"):body.index("  other-reusable-workflow:")]
    assert OIDC_ONLY in job and PROBE_STEP in job and "uses: ./" not in job
    call = body[body.index("  other-reusable-workflow:"):]
    assert "    uses: ./.github/workflows/openai-wif-canary-reusable.yml\n" in call and OIDC_ONLY in call
    assert "\non:\n  workflow_call:\n\n" in other
    assert OIDC_ONLY in other and PROBE_STEP in other
    for text in (canary, other):
        assert "secrets." not in text and "actions/checkout" not in text
