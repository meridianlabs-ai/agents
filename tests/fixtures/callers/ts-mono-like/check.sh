set -euo pipefail
[ ! -e pyproject.toml ] || { echo "::error::this fixture must have no pyproject.toml"; exit 1; }
nv=$($AS_CODEX node --version)
major=${nv#v}; major=${major%%.*}
[ "$major" -ge 22 ] || { echo "::error::expected Node >= 22 (engines), got $nv"; exit 1; }
pv=$($AS_CODEX /home/codex/.local/bin/pnpm --version)
[ "$pv" = "11.22.0" ] || { echo "::error::expected pnpm 11.22.0 from packageManager, got $pv"; exit 1; }
$AS_CODEX node_modules/.bin/prettier --version
# The gate needs pnpm on PATH, not just by absolute path: under sudo's reset
# PATH alone turbo cannot find it (ts-mono's failure before the workflows put
# the bin directories on codex's command PATH); the canary's previous step ran
# the same gate under codex's own command environment.
if out=$($AS_CODEX /home/codex/.local/bin/pnpm check 2>&1); then
  echo "::error::pnpm check passed under sudo's reset PATH; the fixture no longer needs pnpm on PATH"; exit 1
fi
grep -q "Unable to find package manager binary" <<<"$out" || { echo "::error::pnpm check failed under sudo's reset PATH for another reason:"; echo "$out"; exit 1; }
[ "$(sha256sum <pnpm-lock.yaml)" = "$LOCK_BEFORE" ] || { echo "::error::pnpm install changed the lockfile despite --frozen-lockfile"; exit 1; }
echo "ts-mono-like: Node $nv, pnpm $pv via corepack in ~codex/.local/bin, frozen install, prettier runs as codex, turbo gate needs pnpm on PATH"
