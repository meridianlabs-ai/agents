#!/usr/bin/env python3
"""The codex jobs' OpenAI credential: OpenAI API Platform workload identity
federation (design/untrusted-agent-job.md → Codex jobs: OpenAI API Platform
workload identity federation). Stdlib only.

    openai_wif_proxy.py serve --state-dir DIR --identity-provider-id ID \
        --service-account-id ID --audience AUD
    openai_wif_proxy.py stop --state-dir DIR
    openai_wif_proxy.py probe --expect refused|exchanged|any \
        --identity-provider-id ID --service-account-id ID --audience AUD

`serve` requests a GitHub OIDC token for AUD with the step's
`ACTIONS_ID_TOKEN_REQUEST_*` environment and exchanges it at OpenAI's token
endpoint for a short-lived service-account token. The first exchange runs in
the foreground: a failure exits 1 with the exchange's error, never a token.
Then it binds 127.0.0.1 on an ephemeral port, writes
`endpoint=http://127.0.0.1:<port>/v1/responses` to `$GITHUB_OUTPUT`, and
forks a child that serves in its own session, its output going to
`DIR/log`. The child forwards only `POST /v1/responses` to
`https://api.openai.com/v1/responses`, replacing the incoming Authorization
with the current token, and relays the response as bytes, chunk by chunk.
codex-action's own proxy (which accepts only a static key) sends codex's
requests here, with a placeholder key that this forwarder drops.

No refresh token is issued, so the child renews with a fresh OIDC token
whenever the current token is within 60 seconds of its expiry, and once more
when OpenAI answers a request 401 (the request is then retried once). A
failed renewal answers 502 with a fixed message. An upstream redirect is
never followed; it answers 502 too.

The token stays in this process's memory: it is never written to a file,
logged, or returned to a client. The first token and the OIDC token are
masked (`::add-mask::`) in case anything else prints them.

`stop` reports what the child logged (requests, renewals, refusals) on
stdout and in the job summary, and stops it. `probe` is the canaries' one
exchange: it records whether OpenAI exchanged the token, refused it or
could not be reached, and prints only that and the subject's non-secret
claims. The token, if any, is dropped.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import http.server
import json
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse

TOKEN_URL = "https://auth.openai.com/oauth/token"
UPSTREAM_URL = "https://api.openai.com/v1/responses"
ROUTE = "/v1/responses"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
RENEW_MARGIN = 60          # seconds before expiry
MAX_BODY = 32 * 1024 * 1024
CHUNK = 64 * 1024
ATTEMPTS = 3               # OIDC and exchange calls: network errors, 429, 5xx
UPSTREAM_TIMEOUT = 900     # seconds without a byte from OpenAI
RENEWAL_FAILED = b'{"error":{"message":"openai-wif-proxy: token renewal failed","type":"proxy_error"}}'
UPSTREAM_FAILED = b'{"error":{"message":"openai-wif-proxy: upstream request failed","type":"proxy_error"}}'
REDIRECT_REFUSED = b'{"error":{"message":"openai-wif-proxy: upstream redirect refused","type":"proxy_error"}}'
# Hop-by-hop headers (RFC 9110 7.6.1), plus the ones this forwarder sets.
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
       "trailer", "transfer-encoding", "upgrade", "host", "authorization", "content-length", "expect"}


class ExchangeError(Exception):
    """A failed OIDC request or exchange. The message never holds a token."""

    def __init__(self, message: str, refusal: bool = False):
        super().__init__(message)
        self.refusal = refusal  # the exchange answered 4xx: a decision, not an outage


def clean(text: str, limit: int = 200) -> str:
    """One line of printable ASCII, at most `limit` characters."""
    return "".join(c for c in str(text) if " " <= c <= "~")[:limit]


def connection(url: str, timeout: float) -> tuple:
    """An http.client connection for `url` and the path to request. Plain
    HTTP only to loopback (the tests' stubs); everything else is HTTPS with
    the default certificate checks."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.scheme == "https" and parts.hostname:
        return http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout,
                                           context=ssl.create_default_context()), path
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
        return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout), path
    raise ExchangeError(f"refusing URL scheme or host: {parts.scheme}://{parts.hostname}")


def call(method: str, url: str, headers: dict, body: bytes | None, what: str) -> tuple:
    """One small request with retries on network errors, 429 and 5xx; no
    redirect is followed. Returns (status, body)."""
    last = ""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(2 * attempt)
        conn = None
        try:
            conn, path = connection(url, 30)
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(1024 * 1024)
            if resp.status == 429 or resp.status >= 500:
                last = f"{what} answered HTTP {resp.status}"
                continue
            return resp.status, data
        except ExchangeError:
            raise
        except (OSError, http.client.HTTPException) as e:
            last = f"{what} failed: {type(e).__name__}"
        finally:
            if conn is not None:
                conn.close()
    raise ExchangeError(last)


def oidc_token(audience: str) -> str:
    """A GitHub OIDC token for `audience`, from the job's request token."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not url or not bearer:
        raise ExchangeError("no OIDC request token in this job (the job needs `id-token: write`)")
    url += ("&" if "?" in url else "?") + "audience=" + urllib.parse.quote(audience, safe="")
    status, data = call("GET", url, {"Authorization": f"bearer {bearer}", "Accept": "application/json"}, None,
                        "the GitHub OIDC token request")
    if status != 200:
        raise ExchangeError(f"the GitHub OIDC token request answered HTTP {status}")
    try:
        value = json.loads(data)["value"]
    except (ValueError, KeyError, TypeError):
        value = None
    if not isinstance(value, str) or not value:
        raise ExchangeError("the GitHub OIDC token response carried no token")
    return value


def claims(jwt: str) -> dict:
    """The JWT's payload, unverified (the claims are not secret; this is for
    the record only)."""
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        out = json.loads(base64.urlsafe_b64decode(payload))
        return out if isinstance(out, dict) else {}
    except (IndexError, ValueError):
        return {}


def is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def exchange(args, jwt: str) -> tuple:
    """Exchange `jwt` for a service-account token. Returns (token, seconds
    it has left now, the answer's `expires_in` or None). `expires_in` counts from issuance, which is after the
    request started, and `expires_at` is absolute, so the time left is the
    smaller of the two, counted from the request's start and from this
    host's clock; an answer whose token has already expired is an error.
    The error names OpenAI's error code and description only."""
    form = urllib.parse.urlencode({
        "grant_type": GRANT_TYPE,
        "identity_provider_id": args.identity_provider_id,
        "service_account_id": args.service_account_id,
        "subject_token_type": SUBJECT_TOKEN_TYPE,
        "subject_token": jwt,
    }).encode()
    started = time.monotonic()
    status, data = call("POST", args.token_url, {"Content-Type": "application/x-www-form-urlencoded",
                                                 "Accept": "application/json"}, form, "the OpenAI token exchange")
    try:
        doc = json.loads(data)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        doc = {}
    if not 200 <= status < 300:
        err = doc.get("error")
        if isinstance(err, dict):  # {"error": {"code": ..., "message": ...}}
            why = " ".join(str(err.get(k)) for k in ("code", "message") if isinstance(err.get(k), str))
        else:
            why = " ".join(str(doc.get(k)) for k in ("error", "error_description") if isinstance(doc.get(k), str))
        why = clean(why.replace(jwt, "[subject token]")) if why else ""
        raise ExchangeError(f"the OpenAI token exchange answered HTTP {status}" + (f": {why}" if why else ""),
                            refusal=400 <= status < 500)
    token = doc.get("access_token")
    if not isinstance(token, str) or not token or any(not " " < c <= "~" for c in token):
        raise ExchangeError(f"the OpenAI token exchange answered HTTP {status} without a usable access_token")
    left = []
    if is_number(doc.get("expires_in")):
        left.append(doc["expires_in"] - (time.monotonic() - started))
    if is_number(doc.get("expires_at")):
        left.append(doc["expires_at"] - time.time())
    if not left:
        raise ExchangeError(f"the OpenAI token exchange answered HTTP {status} without a usable expiry")
    if min(left) <= 0:
        raise ExchangeError(f"the OpenAI token exchange answered HTTP {status} with an expired token")
    issued = doc["expires_in"] if is_number(doc.get("expires_in")) else None
    return token, float(min(left)), issued


def mask(value: str) -> None:
    print(f"::add-mask::{value}", flush=True)


class Credentials:
    """The current token and its expiry; renewal is serialized."""

    def __init__(self, args, token: str, lifetime: float, log):
        self.args, self.log = args, log
        self.lock = threading.Lock()
        self.token, self.expires = token, time.monotonic() + lifetime

    def _renew(self, why: str) -> None:
        try:
            token, lifetime, _ = exchange(self.args, oidc_token(self.args.audience))
        except ExchangeError as e:
            self.log(f"renewal failed ({why}): {e}")
            raise
        self.token, self.expires = token, time.monotonic() + lifetime
        self.log(f"renewed ({why}): valid for {round(lifetime)} s")

    def current(self) -> str:
        with self.lock:
            if time.monotonic() >= self.expires - RENEW_MARGIN:
                self._renew("expiry")
            return self.token

    def after_401(self, stale: str) -> str:
        with self.lock:
            if self.token == stale:
                self._renew("upstream 401")
            return self.token


class Forwarder(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "openai-wif-proxy"
    sys_version = ""
    timeout = 120  # a silent client frees its thread

    def log_message(self, format, *args):
        pass  # the request line is codex's; only fixed lines go to the log

    def refuse(self, status: int, why: str) -> None:
        self.server.log(f"refused: {why}")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def fail(self, body: bytes) -> None:
        self.close_connection = True
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != ROUTE:
            return self.refuse(403, "not POST /v1/responses")
        if self.headers.get("Transfer-Encoding"):
            return self.refuse(411, "chunked request body")
        length = self.headers.get("Content-Length", "")
        if not (length.isascii() and length.isdigit()):
            return self.refuse(411, "no Content-Length")
        if int(length) > MAX_BODY:
            return self.refuse(413, "request body over the cap")
        body = self.rfile.read(int(length))
        if len(body) != int(length):
            return self.refuse(400, "short request body")
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]
        creds = self.server.creds
        try:
            token = creds.current()
        except ExchangeError:
            return self.fail(RENEWAL_FAILED)
        for attempt in (1, 2):
            try:
                conn, resp = self.upstream(token, headers, body)
            except (OSError, ValueError, http.client.HTTPException) as e:
                self.server.log(f"upstream error: {type(e).__name__}")
                return self.fail(UPSTREAM_FAILED)
            if resp.status == 401 and attempt == 1:
                conn.close()
                try:
                    token = creds.after_401(token)
                except ExchangeError:
                    return self.fail(RENEWAL_FAILED)
                continue
            break
        try:
            if 300 <= resp.status < 400:
                self.server.log(f"upstream error: redirect (HTTP {resp.status}) not followed")
                return self.fail(REDIRECT_REFUSED)
            self.relay(resp)
        finally:
            conn.close()

    def upstream(self, token: str, headers: list, body: bytes):
        conn, path = connection(self.server.upstream_url, UPSTREAM_TIMEOUT)
        try:
            conn.putrequest("POST", path, skip_accept_encoding=True)
            for k, v in headers:
                conn.putheader(k, v)
            conn.putheader("Authorization", f"Bearer {token}")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    def relay(self, resp) -> None:
        """Status, end-to-end headers and the body as it arrives: with the
        upstream's length when it gave one, chunked otherwise (server-sent
        events)."""
        self.close_connection = True
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        self.send_header("Connection", "close")
        if resp.status in (204, 304):  # no body, no framing
            self.end_headers()
            self.server.log(f"forwarded: HTTP {resp.status}, 0 bytes")
            return
        length = resp.getheader("Content-Length", "")
        chunked = resp.chunked or not (length.isascii() and length.isdigit())
        self.send_header("Transfer-Encoding" if chunked else "Content-Length", "chunked" if chunked else length)
        self.end_headers()
        sent = 0
        try:
            while True:
                data = resp.read1(CHUNK)
                if not data:
                    break
                sent += len(data)
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            self.server.log(f"forwarded: HTTP {resp.status}, {sent} bytes")
        except (OSError, http.client.HTTPException) as e:
            self.server.log(f"relay interrupted: HTTP {resp.status}, {sent} bytes, {type(e).__name__}")

    def do_other(self):
        self.refuse(403, "not POST /v1/responses")

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = do_other


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sock: socket.socket, creds: Credentials, upstream_url: str, log):
        super().__init__(sock.getsockname(), Forwarder, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()
        self.creds, self.upstream_url, self.log = creds, upstream_url, log


def write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def summary(line: str) -> None:
    print(line, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(line + "\n")


def serve(args) -> int:
    try:
        os.mkdir(args.state_dir, 0o700)
    except OSError as e:
        print(f"::error::openai-wif-proxy: cannot create the state directory: {type(e).__name__}")
        return 1
    try:
        connection(args.upstream_url, 1)[0].close()
        jwt = oidc_token(args.audience)
        mask(jwt)
        token, lifetime, issued = exchange(args, jwt)
        mask(token)
    except ExchangeError as e:
        print(f"::error::openai-wif-proxy: {e}")
        return 1
    c = claims(jwt)
    oidc_life = c.get("exp", 0) - c.get("iat", 0) if all(isinstance(c.get(k), int) for k in ("exp", "iat")) else None
    del jwt
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port = sock.getsockname()[1]
    fd = os.open(os.path.join(args.state_dir, "log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid:
        sock.close()
        os.close(fd)
        with open(os.path.join(args.state_dir, "pid"), "w") as f:
            f.write(f"{pid}\n")
        write_output("endpoint", f"http://127.0.0.1:{port}{ROUTE}")
        summary(f"openai-wif-proxy: exchanged; token expires_in {issued if issued is not None else 'absent'}, "
                f"valid for {round(lifetime)} s on receipt, GitHub OIDC token "
                f"lifetime (exp - iat) {oidc_life if oidc_life is not None else 'unknown'} s; "
                f"forwarding POST {ROUTE} on 127.0.0.1:{port} (pid {pid})")
        return 0
    # The child: its own session, no tie to the step's output.
    os.setsid()
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(null)
    os.close(fd)
    lock = threading.Lock()

    def log(line: str) -> None:
        with lock:
            os.write(1, f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {line}\n".encode())

    log(f"start: exchanged, valid for {round(lifetime)} s")
    creds = Credentials(args, token, lifetime, log)
    del token
    server = Server(sock, creds, args.upstream_url, log)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        server.serve_forever()
    finally:
        os._exit(0)


def stop(args) -> int:
    log_path = os.path.join(args.state_dir, "log")
    try:
        with open(log_path) as f:
            lines = f.read().splitlines()
    except OSError:
        print("::warning::openai-wif-proxy: no forwarder log")
        lines = []
    count = lambda prefix: sum(1 for line in lines if line.split(" ", 1)[-1].startswith(prefix))
    summary(f"openai-wif-proxy: {count('forwarded:')} requests forwarded, {count('renewed (expiry)')} renewals "
            f"before expiry, {count('renewed (upstream 401)')} after an upstream 401, "
            f"{count('renewal failed')} failed renewals, {count('upstream error')} upstream errors, "
            f"{count('refused:')} refused requests")
    try:
        with open(os.path.join(args.state_dir, "pid")) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    return 0


def probe(args) -> int:
    detail = ""
    try:
        jwt = oidc_token(args.audience)
        mask(jwt)
        c = claims(jwt)
        detail = ", ".join(f"{k}={clean(c.get(k), 120)}" for k in ("aud", "event_name", "job_workflow_ref")
                           if isinstance(c.get(k), str))
        token, _, _ = exchange(args, jwt)
        mask(token)
        outcome, why = "exchanged", "HTTP 2xx, a token (dropped)"
    except ExchangeError as e:
        outcome, why = ("refused" if e.refusal else "error"), str(e)
    line = f"OpenAI token exchange ({detail or 'no subject token'}): {outcome} ({why}); expected {args.expect}"
    summary(line)
    if args.expect != "any" and outcome != args.expect:
        print(f"::error::the OpenAI exchange outcome is {outcome}, expected {args.expect}")
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)
    for name in ("serve", "probe"):
        s = sub.add_parser(name)
        s.add_argument("--identity-provider-id", required=True)
        s.add_argument("--service-account-id", required=True)
        s.add_argument("--audience", required=True)
        # The tests' stubs; the composite never passes these.
        s.add_argument("--token-url", default=TOKEN_URL)
        if name == "serve":
            s.add_argument("--state-dir", required=True)
            s.add_argument("--upstream-url", default=UPSTREAM_URL)
        else:
            s.add_argument("--expect", choices=("refused", "exchanged", "any"), required=True)
    s = sub.add_parser("stop")
    s.add_argument("--state-dir", required=True)
    args = p.parse_args(argv)
    return {"serve": serve, "stop": stop, "probe": probe}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
