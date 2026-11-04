#!/usr/bin/env bash
# End-to-end test against real Lidarr and Vidarr containers. Runs in CI as root on a throwaway
# machine. No indexers or download clients are configured.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
export INSTALL_DIR="$WORK/install" APPDATA="$WORK/appdata" DATA_ROOT="$WORK/data" TZ=Etc/UTC
export SOULSEEK_USERNAME=ci-test-user SOULSEEK_PASSWORD=ci-test-pass
export SLSKD_WEB_USERNAME=admin SLSKD_WEB_PASSWORD=ci-web-pass
MBID="a74b1b7f-71a5-4011-9441-d0b5e4122711" # Radiohead on MusicBrainz

git -c safe.directory='*' -C "$REPO" archive --format=tar.gz -o "$WORK/clippy-music.tar.gz" HEAD
export CLIPPY_TARBALL_URL="file://$WORK/clippy-music.tar.gz"

pass() { echo "ok   - $1"; }
fail() {
  echo "FAIL - $1"
  docker logs --tail 40 clippy-music-sync 2>&1 || true
  docker logs --tail 40 vidarr 2>&1 || true
  docker logs --tail 40 lidarr 2>&1 || true
  exit 1
}

echo '--- installing'
bash "$REPO/install.sh"
env_value() { grep "^$1=" "$INSTALL_DIR/.env" | cut -d= -f2- | tr -d "'"; }
LIDARR_KEY="$(env_value LIDARR_API_KEY)"
VIDARR_KEY="$(env_value VIDARR_API_KEY)"
[ -n "$LIDARR_KEY" ] && pass "installer found the Lidarr key" || fail "no Lidarr key"
[ -n "$VIDARR_KEY" ] && pass "installer found the Vidarr key" || fail "no Vidarr key"

lidarr() { curl -fsS -H "X-Api-Key: $LIDARR_KEY" -H 'Content-Type: application/json' "http://localhost:8686/api/v1$1" "${@:2}"; }
vidarr() { curl -fsS -H "X-Api-Key: $VIDARR_KEY" -H 'Content-Type: application/json' "http://localhost:3434/api/v1$1" "${@:2}"; }
sync_now() { (cd "$INSTALL_DIR" && docker compose run --rm clippy-music-sync python /app/clippy_music.py sync); }

# True once $1 succeeds, within about four minutes.
wait_for() {
  for _ in $(seq 1 48); do
    if eval "$1" >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  return 1
}

echo '--- profiles'
wait_for "lidarr /qualityprofile | jq -e 'any(.[]; .name == \"Clippy Music\")'" && pass "Lidarr has Clippy Music" || fail "Lidarr profile missing"
wait_for "vidarr /qualityprofile | jq -e '[.[].name] | contains([\"Clippy Music Video 1080p\", \"Clippy Music Video 4K\"])'" && pass "Vidarr has both video profiles" || fail "Vidarr profiles missing"
lidarr /tag | jq -e '[.[].label] | contains(["videos", "videos-4k"])' >/dev/null && pass "Lidarr has both tags" || fail "tags missing"

PROFILE="$(lidarr /qualityprofile | jq -c '.[] | select(.name == "Clippy Music")')"
[ "$(jq -r '[.items[] | select(.allowed) | .name] | join(",")' <<<"$PROFILE")" = "Low,192,256,V0,320,Lossless" ] && pass "Lidarr ranks Lossless highest" || fail "wrong ranking: $PROFILE"
[ "$(jq '.cutoff == (.items[-1].id)' <<<"$PROFILE")" = "true" ] && pass "Lidarr stops upgrading at Lossless" || fail "wrong cutoff"

P1080="$(vidarr /qualityprofile | jq '.[] | select(.name == "Clippy Music Video 1080p") | .id')"
P4K="$(vidarr /qualityprofile | jq '.[] | select(.name == "Clippy Music Video 4K") | .id')"

echo '--- adding an artist to Lidarr'
PROFILE_ID="$(jq .id <<<"$PROFILE")"
METADATA_ID="$(lidarr /metadataprofile | jq '.[0].id')"
lidarr /rootfolder -X POST -d "$(jq -n --argjson q "$PROFILE_ID" --argjson m "$METADATA_ID" \
  '{name:"Music", path:"/data/media/music", defaultQualityProfileId:$q, defaultMetadataProfileId:$m, defaultMonitorOption:"none", defaultNewItemMonitorOption:"none", defaultTags:[]}')" >/dev/null \
  && pass "root folder added with Clippy Music" || fail "couldn't add the root folder"

wait_for "lidarr '/artist/lookup?term=lidarr:$MBID' | jq -e 'length > 0'" || fail "Lidarr's metadata lookup is failing"
BODY="$(lidarr "/artist/lookup?term=lidarr:$MBID" | jq -c --argjson q "$PROFILE_ID" --argjson m "$METADATA_ID" \
  '.[0] + {qualityProfileId:$q, metadataProfileId:$m, rootFolderPath:"/data/media/music", monitored:false, tags:[], addOptions:{monitor:"none", searchForMissingAlbums:false}}')"
ARTIST_ID="$(lidarr /artist -X POST -d "$BODY" | jq .id)"
[ -n "$ARTIST_ID" ] && pass "artist added to Lidarr" || fail "couldn't add the artist"

vidarr_artist() { vidarr /artist | jq -c --arg id "$MBID" '.[] | select(.musicbrainzArtistId == $id)'; }
set_tags() { # label, or nothing to clear
  local ids='[]'
  [ -z "${1:-}" ] || ids="$(lidarr /tag | jq -c --arg l "$1" '[.[] | select(.label == $l) | .id]')"
  lidarr "/artist/$ARTIST_ID" | jq -c --argjson t "$ids" '.tags = $t' | lidarr "/artist/$ARTIST_ID" -X PUT -d @- >/dev/null
}
expect() { # description, monitored, profile id
  local artist
  artist="$(vidarr_artist)"
  [ "$(jq -r .monitored <<<"$artist")" = "$2" ] && [ "$(jq -r .qualityProfileId <<<"$artist")" = "$3" ] \
    && pass "$1" || fail "$1: $artist"
}

echo '--- sync'
sync_now || fail "sync reported a failure"
expect "untagged artist reaches Vidarr unmonitored on 1080p" false "$P1080"
set_tags videos; sync_now || fail "sync reported a failure"
expect "the videos tag monitors on 1080p" true "$P1080"
set_tags videos-4k; sync_now || fail "sync reported a failure"
expect "the videos-4k tag monitors on 4K" true "$P4K"
set_tags ""; sync_now || fail "sync reported a failure"
expect "removing the tag stops monitoring" false "$P1080"

echo '--- other containers'
for name in lidarr slskd vidarr clippy-music-sync; do
  [ "$(docker inspect -f '{{.State.Running}}' "$name")" = "true" ] && pass "$name is running" || fail "$name isn't running"
done
if lidarr /system/status >/dev/null && curl -fsS -H "X-Api-Key: $LIDARR_KEY" http://localhost:8686/api/v1/system/plugins >/dev/null 2>&1; then
  pass "Lidarr's plugin API is available"
else
  fail "Lidarr's plugin API is missing, so Tubifarry can't be installed"
fi

echo '--- running the installer again'
cp "$INSTALL_DIR/.env" "$WORK/env.before"
bash "$REPO/install.sh" >/dev/null
cmp -s "$INSTALL_DIR/.env" "$WORK/env.before" && pass "second install keeps the settings" || fail "second install changed .env"

docker logs clippy-music-sync 2>&1 | tail -5
echo 'all passed'
