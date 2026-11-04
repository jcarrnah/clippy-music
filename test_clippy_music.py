"""Tests for clippy_music.py against fake Lidarr and Vidarr servers.

The fakes copy the validation rules in each app's source. They don't prove the real apps behave
the same way, so the first real run should still use --dry-run.

    python test_clippy_music.py
"""
import contextlib
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import clippy_music as cm

CONFIG = json.loads(cm.PROFILES_FILE.read_text(encoding="utf-8"))
ROOT = "/data/media/music-videos"
MB_A = "11111111-1111-1111-1111-111111111111"
MB_B = "22222222-2222-2222-2222-222222222222"
LIDARR_QUALITIES = [
    "Unknown", "MP3-8", "MP3-16", "MP3-24", "MP3-32", "MP3-40", "MP3-48", "MP3-56", "MP3-64",
    "MP3-80", "MP3-96", "MP3-112", "MP3-128", "MP3-160", "MP3-192", "MP3-224", "MP3-256",
    "MP3-320", "MP3-VBR-V2", "MP3-VBR-V0", "AAC-192", "AAC-256", "AAC-320", "AAC-VBR", "WMA",
    "OGG Vorbis Q5", "OGG Vorbis Q6", "OGG Vorbis Q7", "OGG Vorbis Q8", "OGG Vorbis Q9",
    "OGG Vorbis Q10", "WAV", "APE", "WavPack", "FLAC", "ALAC", "FLAC 24bit", "ALAC 24bit",
]


class Fake:
    """A small JSON API. Handlers return (status, body) and record every write."""

    key = "secret"

    def __init__(self):
        self.writes = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                if self.headers.get("X-Api-Key") != fake.key:
                    status, result = 401, {"error": "bad key"}
                else:
                    if self.command != "GET":
                        fake.writes.append((self.command, self.path, body))
                    status, result = fake.route(self.command, self.path.removeprefix("/api/v1"), body)
                raw = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeLidarr(Fake):
    def __init__(self):
        super().__init__()
        self.tags, self.profiles, self.artists = [], [], []

    def validate(self, profile):
        """Mirrors Lidarr's QualityItemsValidator."""
        seen, allowed_ids = [], set()
        for item in profile["items"]:
            if item.get("name"):
                if item["id"] < 1000 or len(item["items"]) < 2:
                    return "bad group"
                seen += [sub["quality"]["name"] for sub in item["items"]]
                if item["allowed"]:
                    allowed_ids.add(item["id"])
            else:
                seen.append(item["quality"]["name"])
                if item["allowed"]:
                    allowed_ids.add(item["quality"]["id"])
        if sorted(seen) != sorted(LIDARR_QUALITIES):
            return "Must contain all qualities exactly once"
        return None if profile["cutoff"] in allowed_ids else "cutoff must be allowed"

    def route(self, method, path, body):
        if (method, path) == ("GET", "/tag"):
            return 200, self.tags
        if (method, path) == ("POST", "/tag"):
            tag = {"id": len(self.tags) + 1, "label": body["label"]}
            self.tags.append(tag)
            return 201, tag
        if (method, path) == ("GET", "/qualityprofile/schema"):
            items = [{"quality": {"id": i, "name": name}, "items": [], "allowed": False}
                     for i, name in enumerate(LIDARR_QUALITIES)]
            return 200, {"name": "", "upgradeAllowed": False, "cutoff": 0, "items": items,
                         "minFormatScore": 0, "cutoffFormatScore": 0, "formatItems": []}
        if (method, path) == ("GET", "/qualityprofile"):
            return 200, self.profiles
        if method in ("POST", "PUT") and path.startswith("/qualityprofile"):
            error = self.validate(body)
            if error:
                return 400, {"error": error}
            if method == "POST":
                body = {**body, "id": len(self.profiles) + 1}
                self.profiles.append(body)
            else:
                self.profiles[0] = body
            return 200, body
        if (method, path) == ("GET", "/artist"):
            return 200, self.artists
        return 404, {"error": f"{method} {path}"}


class FakeVidarr(Fake):
    def __init__(self):
        super().__init__()
        names = ["SD", "720p", "1080p", "2160p", "YouTube"]
        self.qualities = [{"id": i + 1, "name": name} for i, name in enumerate(names)]
        self.roots, self.profiles, self.artists = [], [], []
        self.fail_mbids = set()

    def route(self, method, path, body):
        if (method, path) == ("GET", "/rootfolder"):
            return 200, self.roots
        if (method, path) == ("POST", "/rootfolder"):
            root = {"id": len(self.roots) + 1, "path": body["path"]}
            self.roots.append(root)
            return 201, root
        if (method, path) == ("GET", "/quality"):
            return 200, self.qualities
        if (method, path) == ("GET", "/qualityprofile"):
            return 200, self.profiles
        if (method, path) == ("POST", "/qualityprofile"):
            profile = {**body, "id": len(self.profiles) + 1}
            self.profiles.append(profile)
            return 201, profile
        if (method, path) == ("GET", "/artist"):
            return 200, self.artists
        if (method, path) == ("POST", "/artist/musicbrainz/add"):
            if body["musicbrainzArtistId"] in self.fail_mbids:
                return 502, {"error": "MusicBrainz unavailable"}
            if not any(r["id"] == body["rootFolderId"] for r in self.roots):
                return 400, {"error": "Select a root folder and quality profile first"}
            artist = {"id": len(self.artists) + 1, "musicbrainzArtistId": body["musicbrainzArtistId"],
                      "monitored": False, "qualityProfileId": body["qualityProfileId"]}
            self.artists.append(artist)
            return 200, artist
        if method == "PUT" and path.startswith("/artist/"):
            artist = next(a for a in self.artists if a["id"] == int(path.rsplit("/", 1)[1]))
            # Worst case for Vidarr's partial schema: a missing monitored becomes false.
            artist.update({"monitored": False, **body})
            return 200, artist
        return 404, {"error": f"{method} {path}"}


def lidarr_artist(name, mbid, *tag_labels, fake):
    ids = [t["id"] for t in fake.tags if t["label"] in tag_labels]
    return {"artistName": name, "foreignArtistId": mbid, "tags": ids}


class Base(unittest.TestCase):
    def setUp(self):
        self.lidarr, self.vidarr = FakeLidarr(), FakeVidarr()
        self.addCleanup(self.lidarr.close)
        self.addCleanup(self.vidarr.close)
        self.lidarr_api = cm.Api("Lidarr", self.lidarr.url, Fake.key)
        self.vidarr_api = cm.Api("Vidarr", self.vidarr.url, Fake.key)
        patcher = mock.patch.object(cm, "ADD_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        redirect = contextlib.redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def apply(self, dry_run=False):
        cm.apply_all(self.lidarr_api, self.vidarr_api, CONFIG, ROOT, dry_run)

    def sync(self, dry_run=False):
        return cm.sync(self.lidarr_api, self.vidarr_api, CONFIG, ROOT, dry_run)

    def profile_id(self, name):
        return next(p["id"] for p in self.vidarr.profiles if p["name"] == name)


class ApplyTests(Base):
    def test_lidarr_profile_passes_validation_and_ranks_lossless_highest(self):
        self.apply()
        profile = self.lidarr.profiles[0]
        allowed = [item["name"] for item in profile["items"] if item["allowed"]]
        self.assertEqual(allowed, ["Low", "192", "256", "V0", "320", "Lossless"])
        lossless = profile["items"][-1]
        self.assertEqual(profile["cutoff"], lossless["id"])
        self.assertEqual(lossless["items"][-1]["quality"]["name"], "FLAC 24bit")
        disabled = {i["quality"]["name"] for i in profile["items"] if not i["allowed"]}
        self.assertIn("Unknown", disabled)
        self.assertIn("WAV", disabled)

    def test_creates_tags_root_and_video_profiles_with_fallbacks(self):
        self.apply()
        self.assertEqual({t["label"] for t in self.lidarr.tags}, {"videos", "videos-4k"})
        self.assertEqual(self.vidarr.roots[0]["path"], ROOT)
        names = {q["id"]: q["name"] for q in self.vidarr.qualities}
        allowed = {p["name"]: {names[i["qualityId"]] for i in p["items"] if i["allowed"]}
                   for p in self.vidarr.profiles}
        self.assertEqual(allowed["Clippy Music Video 1080p"], {"1080p", "720p", "YouTube", "SD"})
        self.assertEqual(allowed["Clippy Music Video 4K"], {"2160p", "1080p", "720p", "YouTube", "SD"})

    def test_second_apply_creates_no_duplicates(self):
        self.apply()
        self.apply()
        self.assertEqual(len(self.lidarr.profiles), 1)
        self.assertEqual(len(self.lidarr.tags), 2)
        self.assertEqual(len(self.vidarr.profiles), 2)
        self.assertEqual(len(self.vidarr.roots), 1)

    def test_dry_run_writes_nothing(self):
        self.apply(dry_run=True)
        self.assertEqual(self.lidarr.writes + self.vidarr.writes, [])

    def test_changed_vidarr_profile_warns_and_is_not_overwritten(self):
        self.apply()
        self.vidarr.profiles[0]["cutoffQualityId"] = 1
        self.vidarr.writes.clear()
        self.apply()
        self.assertEqual(self.vidarr.writes, [])
        self.assertIn("WARNING", self.output.getvalue())

    def test_unknown_quality_name_is_rejected(self):
        bad = {"groups": [{"name": "X", "qualities": ["Nope", "FLAC"]}], "cutoff": "X"}
        schema = [{"quality": {"id": 1, "name": "FLAC"}, "items": [], "allowed": False}]
        with self.assertRaisesRegex(cm.SyncError, "Nope"):
            cm.build_lidarr_items(schema, bad)


class SyncTests(Base):
    def setUp(self):
        super().setUp()
        self.apply()

    def test_untagged_artist_is_added_unmonitored_on_1080p(self):
        self.lidarr.artists = [lidarr_artist("A", MB_A, fake=self.lidarr)]
        self.assertEqual(self.sync(), 0)
        artist = self.vidarr.artists[0]
        self.assertFalse(artist["monitored"])
        self.assertEqual(artist["qualityProfileId"], self.profile_id("Clippy Music Video 1080p"))

    def test_tags_control_monitoring_and_profile(self):
        self.lidarr.artists = [lidarr_artist("A", MB_A, "videos", fake=self.lidarr)]
        self.sync()
        artist = self.vidarr.artists[0]
        self.assertTrue(artist["monitored"])
        self.assertEqual(artist["qualityProfileId"], self.profile_id("Clippy Music Video 1080p"))

        self.lidarr.artists = [lidarr_artist("A", MB_A, "videos-4k", fake=self.lidarr)]
        self.sync()
        self.assertTrue(self.vidarr.artists[0]["monitored"])
        self.assertEqual(self.vidarr.artists[0]["qualityProfileId"], self.profile_id("Clippy Music Video 4K"))

        self.lidarr.artists = [lidarr_artist("A", MB_A, fake=self.lidarr)]
        self.sync()
        self.assertFalse(self.vidarr.artists[0]["monitored"])
        self.assertEqual(self.vidarr.artists[0]["qualityProfileId"], self.profile_id("Clippy Music Video 1080p"))

    def test_second_sync_changes_nothing(self):
        self.lidarr.artists = [lidarr_artist("A", MB_A, "videos-4k", fake=self.lidarr)]
        self.sync()
        self.vidarr.writes.clear()
        self.sync()
        self.assertEqual(self.vidarr.writes, [])

    def test_vidarr_only_artist_and_removed_artist_are_left_alone(self):
        self.lidarr.artists = [lidarr_artist("A", MB_A, "videos", fake=self.lidarr)]
        self.sync()
        self.vidarr.artists.append({"id": 99, "musicbrainzArtistId": MB_B, "monitored": True,
                                    "qualityProfileId": 1})
        self.lidarr.artists = []
        self.vidarr.writes.clear()
        self.sync()
        self.assertEqual(self.vidarr.writes, [])
        self.assertEqual(len(self.vidarr.artists), 2)

    def test_one_failure_does_not_stop_the_rest(self):
        self.vidarr.fail_mbids.add(MB_A)
        self.lidarr.artists = [lidarr_artist("A", MB_A, fake=self.lidarr),
                               lidarr_artist("B", MB_B, fake=self.lidarr)]
        self.assertEqual(self.sync(), 1)
        self.assertEqual([a["musicbrainzArtistId"] for a in self.vidarr.artists], [MB_B])

    def test_invalid_mbid_is_skipped(self):
        self.lidarr.artists = [lidarr_artist("A", "not-a-uuid", fake=self.lidarr)]
        self.assertEqual(self.sync(), 1)
        self.assertEqual(self.vidarr.artists, [])

    def test_dry_run_writes_nothing(self):
        self.lidarr.artists = [lidarr_artist("A", MB_A, "videos", fake=self.lidarr)]
        self.vidarr.writes.clear()
        self.assertEqual(self.sync(dry_run=True), 0)
        self.assertEqual(self.vidarr.writes, [])
        self.assertIn("Adding A", self.output.getvalue())

    def test_sync_before_apply_explains_itself(self):
        self.vidarr.profiles.clear()
        with self.assertRaisesRegex(cm.SyncError, "Run apply first"):
            self.sync()

    def test_wrong_key_is_a_clear_error(self):
        wrong = cm.Api("Vidarr", self.vidarr.url, "wrong")
        with self.assertRaisesRegex(cm.SyncError, "HTTP 401"):
            cm.sync(self.lidarr_api, wrong, CONFIG, ROOT, False)


class ComposeTests(unittest.TestCase):
    def test_every_compose_variable_is_in_the_env_example(self):
        import re
        here = Path(__file__).parent
        used = set(re.findall(r"\$\{(\w+)\}", (here / "docker-compose.yml").read_text(encoding="utf-8")))
        defined = set(re.findall(r"^(\w+)=", (here / ".env.example").read_text(encoding="utf-8"), re.M))
        self.assertEqual(used - defined, set())


if __name__ == "__main__":
    unittest.main()
