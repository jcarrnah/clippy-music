#!/usr/bin/env bash
# Install or update the Clippy Music stack on Unraid:
#
#   curl -fsSL https://github.com/jcarrnah/clippy-music/releases/latest/download/install.sh | bash
#
# Running it again updates the stack and keeps your settings. It never changes existing music,
# and it stops rather than replacing containers it didn't create.
#
# Optional overrides: INSTALL_DIR, APPDATA, DATA_ROOT, WAIT_SECONDS.
set -euo pipefail

STACK_CONTAINERS="lidarr slskd vidarr clippy-music-sync"
TARBALL_URL="${CLIPPY_TARBALL_URL:-https://github.com/jcarrnah/clippy-music/releases/latest/download/clippy-music.tar.gz}"
INSTALL_DIR="${INSTALL_DIR:-/mnt/user/appdata/clippy-music}"
WAIT_SECONDS="${WAIT_SECONDS:-180}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

check_prerequisites() {
  [ "$(id -u)" = "0" ] || die "Run this as root, from the Unraid terminal."
  command -v curl >/dev/null || die "curl is required."
  command -v tar >/dev/null || die "tar is required."
  command -v docker >/dev/null || die "Docker isn't available. Enable it under Settings > Docker."
  docker compose version >/dev/null 2>&1 ||
    die "Docker Compose isn't installed. Install the Docker Compose Manager plugin from the Apps tab, then run this again."
}

check_existing_containers() {
  local name project
  for name in $STACK_CONTAINERS; do
    project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$name" 2>/dev/null)" || continue
    [ "$project" = "clippy-music" ] ||
      die "A container named '$name' already exists and wasn't created by this installer. Rename or remove it, then run this again."
  done
}

download_stack() {
  local archive
  archive="$(mktemp)"
  say "Downloading the latest release"
  curl -fsSL "$TARBALL_URL" -o "$archive" || { rm -f "$archive"; die "Couldn't download $TARBALL_URL"; }
  mkdir -p "$INSTALL_DIR"
  tar -xzf "$archive" --no-same-owner -C "$INSTALL_DIR" || { rm -f "$archive"; die "Couldn't unpack the release."; }
  rm -f "$archive"
}

# Prints a value, using the environment if it's set and otherwise asking. Secrets aren't echoed.
ask() {
  local name="$1" prompt="$2" default="${3:-}" secret="${4:-}" value="${!1:-}"
  while [ -z "$value" ]; do
    [ -r /dev/tty ] || die "$name is required, and there's no terminal to ask for it."
    if [ -n "$secret" ]; then
      read -r -s -p "$prompt: " value </dev/tty; echo >/dev/tty
    else
      read -r -p "$prompt${default:+ [$default]}: " value </dev/tty
    fi
    value="${value:-$default}"
  done
  case "$value" in
    *\'*) die "$name can't contain a single quote." ;;
  esac
  printf '%s' "$value"
}

env_get() {
  { grep "^$1=" "$INSTALL_DIR/.env" || true; } | tail -n1 | cut -d= -f2- | sed "s/^'//; s/'$//"
}

env_set() {
  local temp
  temp="$(mktemp "$INSTALL_DIR/.env.XXXXXX")"
  { grep -v "^$1=" "$INSTALL_DIR/.env" || true; } >"$temp"
  printf "%s='%s'\n" "$1" "$2" >>"$temp"
  chmod 600 "$temp"
  mv "$temp" "$INSTALL_DIR/.env"
}

detect_timezone() {
  local zone
  zone="$(readlink /etc/localtime 2>/dev/null | sed -n 's#.*zoneinfo/##p')" || true
  printf '%s' "${zone:-Etc/UTC}"
}

first_time_setup() {
  [ ! -f "$INSTALL_DIR/.env" ] || return 0
  say "First-time setup"
  local data_root soulseek_user soulseek_pass web_user web_pass
  data_root="$(ask DATA_ROOT "Folder for your media and downloads" /mnt/user/data)"
  soulseek_user="$(ask SOULSEEK_USERNAME "Soulseek username")"
  soulseek_pass="$(ask SOULSEEK_PASSWORD "Soulseek password" "" secret)"
  web_user="$(ask SLSKD_WEB_USERNAME "Choose a username for the slskd web page" slskd)"
  web_pass="$(ask SLSKD_WEB_PASSWORD "Choose a password for the slskd web page" "" secret)"

  ( umask 077; cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env" )
  env_set TZ "${TZ:-$(detect_timezone)}"
  env_set APPDATA "${APPDATA:-/mnt/user/appdata}"
  env_set DATA_ROOT "$data_root"
  env_set SOULSEEK_USERNAME "$soulseek_user"
  env_set SOULSEEK_PASSWORD "$soulseek_pass"
  env_set SLSKD_WEB_USERNAME "$web_user"
  env_set SLSKD_WEB_PASSWORD "$web_pass"
}

create_folders() {
  local appdata data_root owner dir
  appdata="$(env_get APPDATA)"
  data_root="$(env_get DATA_ROOT)"
  owner="$(env_get PUID):$(env_get PGID)"
  # Only these folders change owner, and never recursively, so existing files aren't touched.
  for dir in "$appdata/lidarr" "$appdata/slskd" "$appdata/vidarr" \
    "$data_root/media/music" "$data_root/media/music-videos" \
    "$data_root/downloads/soulseek/complete" "$data_root/downloads/soulseek/incomplete"; do
    mkdir -p "$dir"
    chown "$owner" "$dir"
  done
}

read_lidarr_key() {
  sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$(env_get APPDATA)/lidarr/config.xml" 2>/dev/null | head -n1
}

read_vidarr_key() {
  { docker logs vidarr 2>&1 | grep -A2 'Generated a new API key' || true; } | tail -n1 | tr -d '[:space:]'
}

# Saves a key if it's still blank, waiting for the app to generate it on first start.
collect_key() {
  local variable="$1" reader="$2" waited=0 key
  [ -z "$(env_get "$variable")" ] || return 0
  while [ "$waited" -le "$WAIT_SECONDS" ]; do
    key="$("$reader")"
    if [ -n "$key" ]; then
      env_set "$variable" "$key"
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

main() {
  check_prerequisites
  check_existing_containers
  download_stack
  first_time_setup
  create_folders
  cd "$INSTALL_DIR"

  say "Starting the stack. The first run downloads the images, which can take a few minutes."
  docker compose up -d

  say "Connecting Lidarr and Vidarr"
  local missing=""
  collect_key LIDARR_API_KEY read_lidarr_key || missing="Lidarr"
  collect_key VIDARR_API_KEY read_vidarr_key || missing="$missing Vidarr"
  if [ -n "$missing" ]; then
    printf '\nWARNING: Couldn'"'"'t find the API key for:%s\n' " $missing" >&2
    printf 'Add it to %s/.env, then run this installer again.\n' "$INSTALL_DIR" >&2
    exit 1
  fi
  docker compose up -d --force-recreate clippy-music-sync

  local host
  host="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
  host="${host:-your-unraid-ip}"
  say "Installed"
  cat <<EOF
  Lidarr:  http://$host:8686
  slskd:   http://$host:5030
  Vidarr:  http://$host:3434

Vidarr may ask for its API key. To see it:
  grep VIDARR_API_KEY $INSTALL_DIR/.env

Next steps are in the README: https://github.com/jcarrnah/clippy-music#after-installing
Check the sync with: docker logs clippy-music-sync
EOF
}

# Everything runs from here, so a partly downloaded script can't run halfway.
main "$@"
