#!/usr/bin/env bash
# Tests install.sh with stand-ins for docker, curl, id and chown. Nothing real is changed.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
failures=0

check() {
  if eval "$2"; then
    echo "ok   - $1"
  else
    echo "FAIL - $1"
    failures=$((failures + 1))
  fi
}

mkdir -p "$WORK/bin" "$WORK/stack"
cp "$REPO/.env.example" "$REPO/docker-compose.yml" "$REPO/clippy_music.py" "$REPO/profiles.json" "$WORK/stack/"
( cd "$WORK/stack" && tar -czf "$WORK/release.tar.gz" . )

cat >"$WORK/bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "docker $*" >>"$STUB_LOG"
case "$1 ${2:-}" in
  "compose version") [ -z "${NO_COMPOSE:-}" ] ;;
  "compose up") mkdir -p "$STUB_APPDATA/lidarr"; echo '<Config><ApiKey>lidarrkey123</ApiKey></Config>' >"$STUB_APPDATA/lidarr/config.xml" ;;
  "inspect "*) if [ -n "${FOREIGN:-}" ]; then echo ""; else exit 1; fi ;;
  "logs vidarr") if [ -z "${NO_VIDARR_KEY:-}" ]; then printf '\n[vidarr] Generated a new API key (see Settings):\n\n    vidarrkey456\n\nServer listening\n' | head -n4; fi ;;
esac
EOF
printf '#!/usr/bin/env bash\ncp "$CLIPPY_TARBALL_URL" "${@: -1}"\n' >"$WORK/bin/curl"
printf '#!/usr/bin/env bash\necho 0\n' >"$WORK/bin/id"
printf '#!/usr/bin/env bash\necho "chown $*" >>"$STUB_LOG"\n' >"$WORK/bin/chown"
printf '#!/usr/bin/env bash\nexit 0\n' >"$WORK/bin/sleep"
chmod +x "$WORK/bin/"*

run_install() {
  env PATH="$WORK/bin:$PATH" STUB_LOG="$WORK/log" STUB_APPDATA="$WORK/appdata" \
    CLIPPY_TARBALL_URL="$WORK/release.tar.gz" INSTALL_DIR="$WORK/install" APPDATA="$WORK/appdata" \
    DATA_ROOT="$WORK/data" SOULSEEK_USERNAME=me 'SOULSEEK_PASSWORD=pa$$ w#rd' \
    SLSKD_WEB_USERNAME=admin SLSKD_WEB_PASSWORD=webpass WAIT_SECONDS=0 TZ=Etc/UTC \
    "$@" bash "$REPO/install.sh" >"$WORK/out" 2>&1
}

reset() { rm -rf "$WORK/install" "$WORK/appdata" "$WORK/data"; : >"$WORK/log"; }
ENVFILE="$WORK/install/.env"

reset
run_install; code=$?
check "fresh install succeeds" "[ $code = 0 ]"
check "extracts the stack" "[ -f '$WORK/install/docker-compose.yml' ]"
check "fills in the Lidarr key" "grep -q \"^LIDARR_API_KEY='lidarrkey123'\" '$ENVFILE'"
check "fills in the Vidarr key" "grep -q \"^VIDARR_API_KEY='vidarrkey456'\" '$ENVFILE'"
check "keeps special characters in passwords" "grep -qF \"SOULSEEK_PASSWORD='pa\\\$\\\$ w#rd'\" '$ENVFILE'"
check "each setting appears once" "[ \$(grep -c '^DATA_ROOT=' '$ENVFILE') = 1 ]"
check "creates the media folders" "[ -d '$WORK/data/media/music-videos' ] && [ -d '$WORK/data/downloads/soulseek/incomplete' ]"
check "never changes ownership recursively" "! grep -q 'chown -R' '$WORK/log'"
check "restarts the sync with the keys" "grep -q 'up -d --force-recreate clippy-music-sync' '$WORK/log'"

cp "$ENVFILE" "$WORK/env.before"
: >"$WORK/log"
run_install SOULSEEK_USERNAME=someone-else; code=$?
check "second run succeeds" "[ $code = 0 ]"
check "second run keeps your settings" "cmp -s '$ENVFILE' '$WORK/env.before'"

reset
run_install FOREIGN=1; code=$?
check "stops if a container it didn't create exists" "[ $code != 0 ] && grep -q \"wasn't created by this installer\" '$WORK/out'"
check "leaves that container alone" "! grep -q 'compose up' '$WORK/log'"

reset
run_install NO_COMPOSE=1; code=$?
check "explains a missing Compose plugin" "[ $code != 0 ] && grep -q 'Docker Compose Manager' '$WORK/out'"

reset
run_install "SOULSEEK_PASSWORD=it's"; code=$?
check "rejects a single quote in a password" "[ $code != 0 ] && grep -q 'single quote' '$WORK/out'"

reset
run_install NO_VIDARR_KEY=1; code=$?
check "reports a key it can't find" "[ $code != 0 ] && grep -q 'Vidarr' '$WORK/out'"
check "doesn't restart the sync without keys" "! grep -q 'force-recreate' '$WORK/log'"

echo
if [ "$failures" -ne 0 ]; then
  echo "$failures failed"; cat "$WORK/out"; exit 1
fi
echo "all passed"
