#!/usr/bin/env bash
# The engine-isolation canary's OIDC exchange probe
# (design/untrusted-agent-job.md → Testing; called by
# engine-isolation-canary-pipeline.yml in each agent job). As the job's own
# `runner`, request an OIDC token for claude-code-action's audience and POST
# it to the Claude GitHub App token exchange, exactly as the action does when
# it is given no `github_token` (src/github/token.ts). That is what a runner
# compromise in an agent job could do while the App is installed.
#
#   app_token_exchange_probe.sh minted|refused|any
#
# The exchange answers only for events and workflows it accepts: a `push`
# run gets "Invalid OIDC token", and a run whose workflow file differs from
# the default branch's gets "Workflow validation failed" (measured
# 2026-09-30). So the caller expects `minted` only where the exchange would
# answer an agent job, and `any` elsewhere, which records the outcome and
# asserts nothing about it.
#
# `minted`: the exchange returned an App token. The probe revokes it at once
# (DELETE /installation/token) and prints nothing from it. `refused`: no
# token, because the job has no OIDC request token (no `id-token: write`) or
# the exchange answered 4xx. `error`: the exchange could not be reached,
# answered 5xx, or answered 2xx without a token; that is no evidence either
# way, so it fails both `minted` and `refused`. Only the outcome, HTTP
# statuses and a refusal's error message are printed. The
# probe fails when the outcome is not the expected one, and when a minted
# token's revocation fails.
set -uo pipefail

expect=${1:-}
case "$expect" in minted|refused|any) ;; *) echo "usage: $0 minted|refused|any" >&2; exit 2 ;; esac
exchange=${APP_TOKEN_EXCHANGE_URL:-https://api.anthropic.com/api/github/github-app-token-exchange}
api=${GITHUB_API_URL:-https://api.github.com}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { echo "::error::$*"; exit 1; }

revoke_failed=""
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  outcome=refused
  detail="no OIDC request token in this job"
else
  code=$(curl -sS -o "$tmp/oidc" -w '%{http_code}' \
    -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
    "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=claude-code-github-action") || code=000
  [ "$code" = 200 ] || fail "the OIDC token request answered HTTP $code"
  jwt=$(jq -r '.value // empty' "$tmp/oidc" 2>/dev/null)
  rm -f "$tmp/oidc"
  [ -n "$jwt" ] || fail "the OIDC token response carried no token"
  echo "::add-mask::$jwt"
  status=$(curl -sS -o "$tmp/exchange" -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $jwt" "$exchange") || status=000
  unset jwt
  token=$(jq -r '(.token // .app_token // empty) | strings' "$tmp/exchange" 2>/dev/null)
  if [ -n "$token" ]; then
    echo "::add-mask::$token"
    rm -f "$tmp/exchange"
    rc=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE \
      -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
      "$api/installation/token") || rc=000
    unset token
    outcome=minted
    detail="exchange HTTP $status; revoked, HTTP $rc"
    [ "$rc" = 204 ] || revoke_failed=$rc
  else
    case "$status" in 4??) outcome=refused ;; *) outcome=error ;; esac
    detail="exchange HTTP $status"
    # The refusal's reason, which carries no token: the error message, one
    # line of printable characters, at most 200.
    why=$(jq -r '[.error.message?, .message?, .error_code?] | map(strings) | first // empty' "$tmp/exchange" 2>/dev/null \
      | tr -cd '[:print:]' | cut -c1-200)
    [ -z "$why" ] || detail="$detail: $why"
  fi
  rm -f "$tmp/exchange"
fi

line="Claude App token exchange as runner: $outcome ($detail); expected $expect"
echo "$line"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "$line" >>"$GITHUB_STEP_SUMMARY"
[ -z "$revoke_failed" ] || fail "revoking the minted App token answered HTTP $revoke_failed"
[ "$expect" = any ] || [ "$outcome" = "$expect" ] || fail "the exchange outcome is $outcome, expected $expect"
