#!/usr/bin/env python3
"""Keep Lidarr and Vidarr working as one system. Standard library only.

    clippy_music.py apply [--dry-run]   create the profiles and tags from profiles.json
    clippy_music.py sync  [--dry-run]   copy Lidarr's artists to Vidarr and apply their tags
    clippy_music.py run                 apply, then sync on a schedule (the container's command)

Lidarr's tags are the control panel. For every artist in Lidarr:
    no tag       in Vidarr, unmonitored, 1080p profile
    videos       monitored, 1080p profile
    videos-4k    monitored, 4K profile

Lidarr decides for the artists it knows about, so changing their monitoring in Vidarr is undone
by the next sync. Artists that exist only in Vidarr are left alone. Nothing is ever deleted.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROFILES_FILE = Path(__file__).with_name("profiles.json")
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Vidarr looks each new artist up on MusicBrainz, which allows about one request a second.
ADD_DELAY_SECONDS = 1.1


class SyncError(Exception):
    pass


def log(message):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


class Api:
    def __init__(self, name, url, key):
        self.name, self.base, self.key = name, url.rstrip("/"), key

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            f"{self.base}/api/v1{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Api-Key": self.key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read().decode(errors="replace")[:300]
            raise SyncError(f"{self.name} {method} {path}: HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise SyncError(f"{self.name} {method} {path}: {error}") from error
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body):
        return self.call("POST", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)


def by(items, key):
    return {item[key]: item for item in items}


def build_lidarr_items(schema_items, config):
    """Return Lidarr's item list and cutoff id. Lidarr ranks later items higher, so best goes last."""
    known = {}
    for item in schema_items:
        for entry in item.get("items") or [item]:
            known[entry["quality"]["name"]] = entry["quality"]

    def entry(name, allowed):
        if name not in known:
            raise SyncError(f"profiles.json: Lidarr has no quality named '{name}'")
        return {"quality": known[name], "items": [], "allowed": allowed}

    groups, cutoff, wanted = [], None, set()
    for offset, group in enumerate(config["groups"]):
        group_id = 1000 + offset
        wanted.update(group["qualities"])
        groups.append({
            "id": group_id,
            "name": group["name"],
            "allowed": True,
            "items": [entry(name, True) for name in reversed(group["qualities"])],
        })
        if group["name"] == config["cutoff"]:
            cutoff = group_id
    if cutoff is None:
        raise SyncError(f"profiles.json: cutoff '{config['cutoff']}' is not a group")
    disabled = [entry(name, False) for name in known if name not in wanted]
    return disabled + list(reversed(groups)), cutoff


def apply_lidarr(lidarr, config, dry_run):
    wanted_tags = [profile["tag"] for profile in config["vidarr"]]
    existing_tags = {tag["label"].lower() for tag in lidarr.get("/tag")}
    for label in wanted_tags:
        if label.lower() not in existing_tags:
            log(f"Lidarr: creating tag '{label}'")
            if not dry_run:
                lidarr.post("/tag", {"label": label})

    wanted = config["lidarr"]
    schema = lidarr.get("/qualityprofile/schema")
    items, cutoff = build_lidarr_items(schema["items"], wanted)
    current = by(lidarr.get("/qualityprofile"), "name").get(wanted["name"])
    base = current or schema
    profile = {**base, "name": wanted["name"], "upgradeAllowed": wanted["upgradeAllowed"],
               "cutoff": cutoff, "items": items}
    if current is None:
        log(f"Lidarr: creating profile '{wanted['name']}'")
        if not dry_run:
            lidarr.post("/qualityprofile", profile)
    elif current != profile:
        log(f"Lidarr: updating profile '{wanted['name']}'")
        if not dry_run:
            lidarr.put(f"/qualityprofile/{current['id']}", profile)


def vidarr_profile_body(wanted, qualities):
    names = {quality["name"]: quality["id"] for quality in qualities}
    for name in [wanted["cutoff"], *wanted["allowed"]]:
        if name not in names:
            raise SyncError(f"profiles.json: Vidarr has no quality named '{name}'")
    if wanted["cutoff"] not in wanted["allowed"]:
        raise SyncError(f"profiles.json: {wanted['name']}: the cutoff must be an allowed quality")
    return {
        "name": wanted["name"],
        "cutoffQualityId": names[wanted["cutoff"]],
        "items": [{"qualityId": quality_id, "allowed": name in wanted["allowed"]}
                  for name, quality_id in names.items()],
    }


def apply_vidarr(vidarr, config, video_root, dry_run):
    if not any(folder["path"].rstrip("/") == video_root.rstrip("/")
               for folder in vidarr.get("/rootfolder")):
        log(f"Vidarr: creating root folder {video_root}")
        if not dry_run:
            vidarr.post("/rootfolder", {"path": video_root})

    qualities = vidarr.get("/quality")
    existing = by(vidarr.get("/qualityprofile"), "name")
    for wanted in config["vidarr"]:
        body = vidarr_profile_body(wanted, qualities)
        current = existing.get(wanted["name"])
        if current is None:
            log(f"Vidarr: creating profile '{wanted['name']}'")
            if not dry_run:
                vidarr.post("/qualityprofile", body)
            continue
        # Vidarr's API can't edit a profile, and deleting one could affect its artists.
        allowed = {item["qualityId"] for item in current["items"] if item["allowed"]}
        if (current["cutoffQualityId"] != body["cutoffQualityId"]
                or allowed != {item["qualityId"] for item in body["items"] if item["allowed"]}):
            log(f"WARNING Vidarr: '{wanted['name']}' differs from profiles.json. "
                "Change it in Vidarr's Quality Profiles page.")


def desired_state(artist, profiles):
    """Return (monitored, profile id) for a Lidarr artist. The last matching profile wins."""
    monitored, profile_id = False, profiles[0]["id"]
    for profile in profiles:
        if profile["tag_id"] in artist.get("tags", []):
            monitored, profile_id = True, profile["id"]
    return monitored, profile_id


def sync(lidarr, vidarr, config, video_root, dry_run):
    tags = {tag["label"].lower(): tag["id"] for tag in lidarr.get("/tag")}
    vidarr_profiles = by(vidarr.get("/qualityprofile"), "name")
    profiles = []
    for wanted in config["vidarr"]:
        if wanted["name"] not in vidarr_profiles:
            raise SyncError(f"Vidarr has no profile '{wanted['name']}'. Run apply first.")
        profiles.append({"id": vidarr_profiles[wanted["name"]]["id"],
                         "tag_id": tags.get(wanted["tag"].lower())})
    roots = [folder for folder in vidarr.get("/rootfolder")
             if folder["path"].rstrip("/") == video_root.rstrip("/")]
    if not roots:
        raise SyncError(f"Vidarr has no root folder {video_root}. Run apply first.")

    known = {artist["musicbrainzArtistId"].lower(): artist for artist in vidarr.get("/artist")}
    counts = {"added": 0, "changed": 0, "failed": 0}
    for artist in lidarr.get("/artist"):
        name, mbid = artist.get("artistName", "?"), (artist.get("foreignArtistId") or "").lower()
        if not MBID.match(mbid):
            log(f"Skipping {name}: no valid MusicBrainz ID")
            counts["failed"] += 1
            continue
        monitored, profile_id = desired_state(artist, profiles)
        try:
            current = known.get(mbid)
            if current is None:
                log(f"Adding {name} to Vidarr")
                counts["added"] += 1
                if dry_run:
                    current = {"monitored": False, "qualityProfileId": profiles[0]["id"], "id": None}
                else:
                    current = vidarr.post("/artist/musicbrainz/add", {
                        "musicbrainzArtistId": mbid,
                        "rootFolderId": roots[0]["id"],
                        "qualityProfileId": profiles[0]["id"],
                    })
                    time.sleep(ADD_DELAY_SECONDS)
            if current["monitored"] != monitored or current["qualityProfileId"] != profile_id:
                # Always send both: Vidarr's validation may default a missing monitored to false.
                changes = {"monitored": monitored, "qualityProfileId": profile_id}
                log(f"Updating {name}: {changes}")
                counts["changed"] += 1
                if not dry_run:
                    vidarr.put(f"/artist/{current['id']}", changes)
        except SyncError as error:
            log(f"Failed on {name}: {error}")
            counts["failed"] += 1
    log(f"Sync finished: {counts['added']} added, {counts['changed']} changed, "
        f"{counts['failed']} failed{' (dry run, nothing written)' if dry_run else ''}")
    return counts["failed"]


def apply_all(lidarr, vidarr, config, video_root, dry_run):
    apply_lidarr(lidarr, config, dry_run)
    apply_vidarr(vidarr, config, video_root, dry_run)


def run_forever(lidarr, vidarr, config, video_root, minutes):
    applied = False
    while True:
        try:
            if not lidarr.key or not vidarr.key:
                raise SyncError("LIDARR_API_KEY and VIDARR_API_KEY must be set in .env")
            if not applied:
                apply_all(lidarr, vidarr, config, video_root, False)
                applied = True
            sync(lidarr, vidarr, config, video_root, False)
        except SyncError as error:
            log(f"ERROR {error}")
        time.sleep(minutes * 60)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["apply", "sync", "run"])
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args(argv)

    env = os.environ
    lidarr = Api("Lidarr", env.get("LIDARR_URL", "http://localhost:8686"), env.get("LIDARR_API_KEY", ""))
    vidarr = Api("Vidarr", env.get("VIDARR_URL", "http://localhost:3434"), env.get("VIDARR_API_KEY", ""))
    video_root = env.get("VIDEO_ROOT", "/data/media/music-videos")
    config = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))

    if args.command == "run":
        run_forever(lidarr, vidarr, config, video_root, int(env.get("SYNC_INTERVAL_MINUTES", "60")))
        return 0
    try:
        if args.command == "apply":
            apply_all(lidarr, vidarr, config, video_root, args.dry_run)
            return 0
        return 1 if sync(lidarr, vidarr, config, video_root, args.dry_run) else 0
    except SyncError as error:
        log(f"ERROR {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
