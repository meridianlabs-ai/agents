#!/bin/bash
# Hosted-runner smoke test for the codex path's runner-side search path
# (Claude Security finding 4628448; design/codex-engine.md → Runner-side
# search path). The unit tests in test_codex_path.py stub `sudo` and have no
# codex user; this runs the SAME composite bodies, lifted from the checked-out
# revision, on a real Ubuntu runner with real identities, the real image PATH
# and real sudo — `.github/workflows/codex-path-smoke.yml` runs it, and it
# runs by hand on any Ubuntu box with passwordless sudo from the repo root:
#
#     GITHUB_WORKSPACE=$PWD RUNNER_TEMP=$(mktemp -d) bash tests/codex_path_smoke.sh
#
# SMOKE_USER=claude-agent runs the same cases for the Claude jobs' agent user
# (the default is codex): every probe, ownership and refusal names that user,
# codex-usage is skipped, and step 4b checks that the launcher's root-owned
# /opt/meridian-agent/bin passes the check on the job PATH and cannot be
# planted into.
#
# What it establishes, in order (every step logs what it did; any failure
# exits non-zero):
#
#   1. `create-codex-user` step 1 (snapshots, user, groups) runs.
#   2. The pre-grant check (`assert-runner-only-path`, protect on) PASSES on
#      the stock job PATH — protecting the image's world-writable hops
#      (/opt, /opt/pipx_bin, the toolcache, /usr/local/bin) rather than
#      refusing them — and afterwards the codex user can create nothing in
#      them while the runner user still can (npm's global bin).
#   3. The same check REFUSES a workspace venv on the job PATH, and an
#      outward symlink from the workspace to /usr/bin (review round 1 of
#      #131); and, with real codex ownership (review round 2): a
#      codex-owned mode-555 directory and a codex-owned sticky directory
#      holding a runner-owned `bin` are refused without protect and
#      protected (chown to runner) with it — after which codex cannot chmod
#      them back; a runner-only directory holding `bash -> <workspace
#      venv>/bash` is refused in both modes; one holding `bash -> <a
#      codex-owned 0755 file>` is refused without protect and the file
#      protected with it; and (review round 3) a safe child of a
#      runner-owned sticky directory does not vouch for a missing sibling,
#      the directory itself or a dangling link into it — each refused after
#      the safe child, the directory protected with protect on.
#   4. `create-codex-user` step 3 (the grant and the rest) runs.
#   5. As the codex user: plant `sudo`, `bash`, `git`, `find`, `jq` in the
#      workspace venv, tamper with .git/config, and try to plant into the
#      protected directories (must fail).
#   6. `reclaim-codex-workspace` — its nested check (no protect), then its
#      script — completes with real sudo/pkill, restores the config, and none
#      of the planted files ran. Then `codex-usage` and `unresolved-merge-guard`
#      complete the same way.
#
# It does not run codex-action or codex itself (no API key, and the boundary
# under test is the runner-side PATH, not the model). The workflow's
# `mechanism` job separately shows the runner picking a planted interpreter
# when a workspace venv IS on GITHUB_PATH, which is what the refusal in 3
# prevents.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
# The agent user under test: codex (the default) or claude-agent, the Claude
# jobs' user (design/executed-paths-residual.md → The agent user).
U=${SMOKE_USER:-codex}
case "$U" in codex|claude-agent) ;; *) echo "::error::smoke: SMOKE_USER must be codex or claude-agent" >&2; exit 1 ;; esac
: "${GITHUB_WORKSPACE:?set GITHUB_WORKSPACE (the checkout the codex user is granted)}"
: "${RUNNER_TEMP:?set RUNNER_TEMP}"
SYSTEM_PATH=/usr/sbin:/usr/bin:/sbin:/bin
hijack_log=/tmp/codex-path-smoke.hijack.log
rm -f "$hijack_log"

say() { printf '\n==> %s\n' "$*"; }
fail() { echo "::error::smoke: $*" >&2; exit 1; }

say "revision $(git -C "$root" rev-parse HEAD) on ${ImageOS:-?}/${ImageVersion:-?}; workspace $GITHUB_WORKSPACE; user $(id -un) ($(id))"
echo "job PATH: $PATH"

# The Nth `run: |` block of a composite, dedented — the same extraction the
# unit tests use, so what runs here is the reviewed body of this revision.
lift() { # <action dir name> <block number, 1-based>
  python3 - "$root/.github/actions/$1/action.yml" "$2" <<'PY'
import sys
lines = open(sys.argv[1]).read().splitlines()
blocks = []
for i, line in enumerate(lines):
    if line != "      run: |":
        continue
    body = []
    for later in lines[i + 1:]:
        if later.strip() == "":
            body.append("")
        elif later.startswith("        "):
            body.append(later[8:])
        else:
            break
    blocks.append("\n".join(body) + "\n")
sys.stdout.write(blocks[int(sys.argv[2]) - 1])
PY
}
# From a file, as the runner runs a step (`bash -e <file>`): the check
# re-executes `$0` under sudo once.
lift assert-runner-only-path 1 >"$RUNNER_TEMP/assert-runner-only-path.sh"
run_assert() { # <PATH> <protect> [user]
  PATH="$1" USER_NAME="${3:-$U}" PROTECT="$2" SYSTEM_PATH="$SYSTEM_PATH" /bin/bash -e "$RUNNER_TEMP/assert-runner-only-path.sh"
}

say "1. create-codex-user, step 1: snapshots, user, groups"
AGENT_USER="$U" GRANT=workspace /bin/bash -c "$(lift create-codex-user 1)"
id "$U"
test -f "$RUNNER_TEMP/git-config.pre-codex" || fail "no config snapshot"

say "2. pre-grant check with protect on, stock job PATH — must pass"
job_path="$PATH"
t0=$(date +%s)
before=$(for d in /opt /opt/pipx_bin /usr/local/bin; do [ -d "$d" ] && stat -c '%A %U %n' "$d"; done || true)
echo "before: $before"
run_assert "$job_path" true | tee "$RUNNER_TEMP/smoke-assert-stock.log"
after=$(for d in /opt /opt/pipx_bin /usr/local/bin; do [ -d "$d" ] && stat -c '%A %U %n' "$d"; done || true)
echo "after: $after"
grep -q "none inside the workspace or owned or writable by $U" "$RUNNER_TEMP/smoke-assert-stock.log" || fail "stock PATH did not pass"
echo "pre-grant check took $(( $(date +%s) - t0 )) s"
say "2b. the protected hops: $U cannot create in them, runner can"
for d in /opt/pipx_bin /usr/local/bin; do
  [ -d "$d" ] || continue
  if sudo -u "$U" sh -c "echo x >'$d/sudo'" 2>/dev/null; then fail "$U could write $d/sudo"; fi
  echo "$U cannot write $d: ok"
  touch "$d/.codex-path-smoke" && rm "$d/.codex-path-smoke" && echo "runner still writes $d: ok"
done
say "2c. the runner's npm global install still works after protection (what openai/codex-action does)"
npm install -g @openai/codex >"$RUNNER_TEMP/smoke-npm.log" 2>&1 || { tail -20 "$RUNNER_TEMP/smoke-npm.log"; fail "npm install -g failed after protection"; }
command -v codex && echo "$U CLI installed at $(command -v codex): ok"
say "2d. every entry of the job PATH after protection, for the record"
printf '%s\n' "$job_path" | tr ':' '\n' | while IFS= read -r e; do [ -e "$e" ] && stat -L -c '%A %U:%G %n' "$e" || echo "(missing) $e"; done

say "3. the check refuses a workspace venv on PATH and an outward workspace symlink"
mkdir -p "$GITHUB_WORKSPACE/.venv/bin"
if run_assert "$GITHUB_WORKSPACE/.venv/bin:$job_path" true >"$RUNNER_TEMP/smoke-refuse-venv.log" 2>&1; then fail "workspace venv on PATH was not refused"; fi
grep -q "lies inside the workspace" "$RUNNER_TEMP/smoke-refuse-venv.log" || { cat "$RUNNER_TEMP/smoke-refuse-venv.log"; fail "wrong refusal"; }
echo "refused: $(grep -o "job PATH entry '[^']*' lies inside the workspace[^,]*" "$RUNNER_TEMP/smoke-refuse-venv.log")"
ln -s /usr/bin "$GITHUB_WORKSPACE/tools"
if run_assert "$GITHUB_WORKSPACE/tools:$job_path" true >"$RUNNER_TEMP/smoke-refuse-link.log" 2>&1; then fail "outward workspace symlink on PATH was not refused"; fi
grep -q "lies inside the workspace" "$RUNNER_TEMP/smoke-refuse-link.log" || { cat "$RUNNER_TEMP/smoke-refuse-link.log"; fail "wrong refusal"; }
echo "refused: $(grep -o "job PATH entry '[^']*' lies inside the workspace[^,]*" "$RUNNER_TEMP/smoke-refuse-link.log")"
rm "$GITHUB_WORKSPACE/tools"

say "3b. real ownership (review round 2): owned directories and linked executables"
T=$RUNNER_TEMP/r2
mkdir -p "$T"
sudo install -d -o "$U" -g "$U" -m 555 "$T/owned555"
sudo install -d -o "$U" -g "$U" -m 1777 "$T/ownedsticky"
install -d -m 755 "$T/ownedsticky/bin"
install -d -m 755 "$T/linkws" "$T/linkowned" "$T/ownedfile"
ln -s "$GITHUB_WORKSPACE/.venv/bin/bash" "$T/linkws/bash"
printf '#!/bin/bash\nexit 0\n' >"$T/ownedfile/bash"; chmod 755 "$T/ownedfile/bash"; sudo chown "$U:$U" "$T/ownedfile/bash"
ln -s "$T/ownedfile/bash" "$T/linkowned/bash"
expect_refusal() { # <label> <PATH> <protect> <expected text>
  if run_assert "$2" "$3" >"$RUNNER_TEMP/smoke-r2-$1.log" 2>&1; then cat "$RUNNER_TEMP/smoke-r2-$1.log"; fail "$1 (protect=$3) was not refused"; fi
  grep -q "$4" "$RUNNER_TEMP/smoke-r2-$1.log" || { cat "$RUNNER_TEMP/smoke-r2-$1.log"; fail "$1: wrong refusal"; }
  echo "$1 (protect=$3) refused: $(grep -o "job PATH entry '[^']*' [^—]*" "$RUNNER_TEMP/smoke-r2-$1.log" | head -1)"
}
expect_pass() { # <label> <PATH> <protect> <expected text>
  run_assert "$2" "$3" >"$RUNNER_TEMP/smoke-r2-$1.log" 2>&1 || { cat "$RUNNER_TEMP/smoke-r2-$1.log"; fail "$1 (protect=$3) did not pass"; }
  grep -q "$4" "$RUNNER_TEMP/smoke-r2-$1.log" || { cat "$RUNNER_TEMP/smoke-r2-$1.log"; fail "$1: expected '$4'"; }
  echo "$1 (protect=$3) passed: $(grep -o "protected job PATH [a-z]* [^ ]*" "$RUNNER_TEMP/smoke-r2-$1.log" | tr '\n' ';')"
}
expect_refusal owned555 "$T/owned555:$job_path" false "is owned by the $U user (at $T/owned555)"
expect_pass owned555 "$T/owned555:$job_path" true "protected job PATH hop $T/owned555"
stat -c '%A %U:%G %n' "$T/owned555"
if sudo -u "$U" chmod 777 "$T/owned555" 2>/dev/null; then fail "$U could chmod the protected directory back"; fi
echo "$U cannot chmod $T/owned555 after protection: ok"
expect_refusal ownedsticky "$T/ownedsticky/bin:$job_path" false "is owned by the $U user (at $T/ownedsticky)"
expect_pass ownedsticky "$T/ownedsticky/bin:$job_path" true "protected job PATH hop $T/ownedsticky"
expect_refusal linkws "$T/linkws:$job_path" false "lies inside the workspace"
expect_refusal linkws "$T/linkws:$job_path" true "lies inside the workspace"
expect_refusal linkowned "$T/linkowned:$job_path" false "is owned by the $U user (at $T/ownedfile/bash)"
expect_pass linkowned "$T/linkowned:$job_path" true "protected job PATH file $T/ownedfile/bash"
stat -c '%A %U:%G %n' "$T/ownedfile/bash"
if sudo -u "$U" sh -c "echo x >>'$T/ownedfile/bash'" 2>/dev/null; then fail "$U could still write the protected file"; fi
echo "$U cannot write $T/ownedfile/bash after protection: ok"
# Review round 3: a sticky directory accepted for one child must not be
# remembered as safe for a missing sibling, for itself, or for a dangling
# link into it.
install -d -m 1777 "$T/sticky"
install -d -m 755 "$T/sticky/runner-bin" "$T/danglers"
ln -s "$T/sticky/not-yet/bash" "$T/danglers/bash"
expect_pass sticky-safe "$T/sticky/runner-bin:$job_path" false "job PATH:"
expect_refusal sticky-missing "$T/sticky/runner-bin:$T/sticky/not-yet/bin:$job_path" false "is writable by the $U user (at $T/sticky)"
expect_refusal sticky-itself "$T/sticky/runner-bin:$T/sticky:$job_path" false "is writable by the $U user (at $T/sticky)"
expect_refusal sticky-dangling "$T/sticky/runner-bin:$T/danglers:$job_path" false "is writable by the $U user (at $T/sticky)"
stat -c '%A %U:%G %n' "$T/sticky"
expect_pass sticky-missing "$T/sticky/runner-bin:$T/sticky/not-yet/bin:$job_path" true "protected job PATH hop $T/sticky"
if sudo -u "$U" mkdir "$T/sticky/not-yet" 2>/dev/null; then fail "$U could still create in the protected sticky directory"; fi
echo "$U cannot create $T/sticky/not-yet after protection: ok"
# The stock PATH passes again with nothing left to protect.
t0=$(date +%s)
run_assert "$job_path" false >"$RUNNER_TEMP/smoke-r2-stock-again.log" || { cat "$RUNNER_TEMP/smoke-r2-stock-again.log"; fail "stock PATH no longer passes"; }
grep -o 'job PATH: .*' "$RUNNER_TEMP/smoke-r2-stock-again.log"
echo "check without protect took $(( $(date +%s) - t0 )) s"

say "4. create-codex-user, step 3 (its second run block): the grant and the rest"
# HOME_SCRIPT, AGENT_USER and GRANT are the step's env (the last two its
# defaults): the grant step ends with the shared codex-home.sh (also run by
# the composite's reset-home mode).
HOME_SCRIPT="$root/.github/actions/create-codex-user/codex-home.sh" AGENT_USER="$U" GRANT=workspace \
  /bin/bash -c "$(lift create-codex-user 2)"
stat -c '%A %U:%G %n' "$GITHUB_WORKSPACE" "$GITHUB_WORKSPACE/.venv/bin"

if [ "$U" = claude-agent ]; then
  say "4b. claude-agent: the launcher's root-owned home passes the check on the job PATH"
  stat -c '%A %U:%G %n' /opt/meridian-agent "$RUNNER_TEMP/claude-agent"
  [ "$(stat -c '%U:%G %a' /opt/meridian-agent)" = "root:root 755" ] || fail "/opt/meridian-agent is not root:root 755"
  [ "$(stat -c '%U:%G %a' "$RUNNER_TEMP/claude-agent")" = "runner:claude-agent 2775" ] || fail "the landing dir is not runner:claude-agent 2775"
  sudo install -d -o root -g root -m 755 /opt/meridian-agent/bin
  run_assert "/opt/meridian-agent/bin:$job_path" false >"$RUNNER_TEMP/smoke-launcher-bin.log" 2>&1 || { cat "$RUNNER_TEMP/smoke-launcher-bin.log"; fail "the launcher's bin directory on the job PATH was refused"; }
  echo "/opt/meridian-agent/bin on the job PATH passes: ok"
  if sudo -u claude-agent sh -c "echo x >/opt/meridian-agent/bin/claude" 2>/dev/null; then fail "claude-agent could plant into /opt/meridian-agent/bin"; fi
  echo "claude-agent cannot plant into /opt/meridian-agent/bin: ok"
fi

say "5. as $U: plant into the venv, tamper with .git/config, try the protected dirs"
sudo -u "$U" bash -c '
  set -e
  ws="$1"; log="$2"
  for t in sudo bash git find jq; do
    printf "#!/bin/bash\necho HIJACKED:%s >>%s\nexec /usr/bin/%s \"\$@\"\n" "$t" "$log" "$t" >"$ws/.venv/bin/$t"
    chmod +x "$ws/.venv/bin/$t"
  done
  git -C "$ws" config core.sshCommand "echo TAMPERED"
  ls -l "$ws/.venv/bin"
  for d in /opt/pipx_bin /usr/local/bin; do
    [ -d "$d" ] || continue
    if echo x >"$d/sudo" 2>/dev/null; then echo "::error::codex planted $d/sudo"; exit 1; fi
    echo "$U cannot plant into $d: ok"
  done
' _ "$GITHUB_WORKSPACE" "$hijack_log"
grep -q TAMPERED "$GITHUB_WORKSPACE/.git/config" || fail "tamper did not land"

say "6. reclaim: nested check (no protect) then the reclaim script, with the planted venv NOT on PATH (as in a real job)"
run_assert "$job_path" false
# The step's env: its run block pins PATH and runs reclaim.sh.
reclaim_env=(AGENT_USER="$U" SNAPSHOT='' EMBEDDED_SNAPSHOT='' SYSTEM_PATH="$SYSTEM_PATH"
             RECLAIM_SCRIPT="$root/.github/actions/reclaim-codex-workspace/reclaim.sh")
env "${reclaim_env[@]}" /bin/bash -c "$(lift reclaim-codex-workspace 1)"
grep -q TAMPERED "$GITHUB_WORKSPACE/.git/config" && fail "config not restored"
grep -q 'fsmonitor = false' "$GITHUB_WORKSPACE/.git/config" || fail "pins not appended"
stat -c '%A %U:%G %n' "$GITHUB_WORKSPACE" "$GITHUB_WORKSPACE/.git"
say "6b. codex-usage (no sessions: reports nothing) and the guard"
summary=$RUNNER_TEMP/smoke-summary.md; out=$RUNNER_TEMP/smoke-output.txt; : >"$summary"; : >"$out"
if [ "$U" = codex ]; then CODEX_HOME_DIR=/home/codex/.codex REQ_MODEL='' REQ_EFFORT='' SYSTEM_PATH="$SYSTEM_PATH" GITHUB_STEP_SUMMARY="$summary" GITHUB_OUTPUT="$out" /bin/bash -c "$(lift codex-usage 1)"; fi
CONFLICTS='' GIT_DIR="$GITHUB_WORKSPACE/.git" GIT_COMMON_DIR="$GITHUB_WORKSPACE/.git" GIT_WORK_TREE="$GITHUB_WORKSPACE" GIT_CONFIG_GLOBAL=/dev/null \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false SYSTEM_PATH="$SYSTEM_PATH" /bin/bash -c "$(lift unresolved-merge-guard 1)"

say "6c. and once more with the planted venv FIRST on this shell's PATH — the pins hold even then"
/usr/bin/env PATH="$GITHUB_WORKSPACE/.venv/bin:$job_path" "${reclaim_env[@]}" /bin/bash -c "$(lift reclaim-codex-workspace 1)"
[ "$U" != codex ] || PATH="$GITHUB_WORKSPACE/.venv/bin:$job_path" CODEX_HOME_DIR=/home/codex/.codex REQ_MODEL='' REQ_EFFORT='' SYSTEM_PATH="$SYSTEM_PATH" GITHUB_STEP_SUMMARY="$summary" GITHUB_OUTPUT="$out" /bin/bash -c "$(lift codex-usage 1)"

if [ -e "$hijack_log" ]; then cat "$hijack_log"; fail "a planted file ran"; fi
say "no planted file ran. smoke passed."
