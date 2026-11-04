# Clippy Music

Music and music videos on Unraid, managed from one place. This stack runs
[Lidarr](https://github.com/Lidarr/Lidarr) and [slskd](https://github.com/slskd/slskd) for music,
[Vidarr](https://github.com/chese79/Vidarr) for music videos, and a small sync that connects them.

## Install

You need Docker enabled and the **Docker Compose Manager** plugin from the Apps tab. Then run this in
the Unraid terminal:

```bash
curl -fsSL https://github.com/jcarrnah/clippy-music/releases/latest/download/install.sh | bash
```

It asks for your media folder, your Soulseek login and a password for the slskd page. It then starts
everything, connects Lidarr and Vidarr, and creates the profiles and tags.

Run the same command to update. It keeps your settings and doesn't change your media. To update the
apps themselves, run `docker compose pull && docker compose up -d` in
`/mnt/user/appdata/clippy-music`.

## Using it

**Lidarr is the one place you manage everything.** Add an artist there to get their albums. Add a tag
if you also want their music videos:

| Lidarr tag | Music videos |
| :-- | :-- |
| None | Listed in Vidarr, not downloaded automatically |
| `videos` | Downloaded, up to 1080p |
| `videos-4k` | Downloaded, up to 4K |

The sync runs hourly. Removing a tag stops new automatic video downloads, but keeps videos you
already have.

**Watch your storage when adding artists.** Choosing "All Albums" can download a whole discography in
FLAC. "Latest Album" or "None" lets you pick albums yourself.

## After installing

The installer can't do these steps for you, because they depend on your accounts and setup.

### 1. Lidarr, at port 8686

- **Settings > Media Management:** add the root folder `/data/media/music`, with **Clippy Music** as
  its default quality profile.
- **Settings > Download Clients:** add SABnzbd or qBittorrent.
- **Settings > Indexers:** add your indexers, or add Lidarr as an app in Prowlarr.

Your download clients need to see files at the same paths as these containers, or imports fail. The
simplest setup is to mount your media folder as `/data` in them too. Otherwise, add a Remote Path
Mapping in Lidarr. If they're on another Docker network, use your Unraid IP rather than a container
name.

### 2. Soulseek

- In slskd, at port 5030, create an API key.
- In Lidarr, go to **System > Plugins** and install `https://github.com/TypNull/Tubifarry`.
- Add slskd as both a **download client** and an **indexer**, at `http://slskd:5030`.
- Turn on Tubifarry's **Search Sniper**. Soulseek may not feed new releases to Lidarr automatically,
  so this searches for wanted albums in batches.
- Forward port 50300 on your router.

Nothing is shared by default, but many Soulseek users won't send files to people who share nothing.
To share, add `SLSKD_SHARED_DIR` to `slskd` in the Compose file. This makes those files visible to
other Soulseek users, so it's your decision.

### 3. Vidarr, at port 3434

If it asks for its API key, run `grep VIDARR_API_KEY /mnt/user/appdata/clippy-music/.env`.

- **Settings > General:** add an [IMVDb](https://imvdb.com) API key.
- **Settings > Indexers:** add your Torznab and Newznab indexers.
- **Settings > Download Clients:** add SABnzbd or qBittorrent.

### 4. Plex or Jellyfin

Add `media/music` as a music library, and `media/music-videos` as a separate video library.

### 5. Test with one artist

1. Add one artist in Lidarr. They should appear in Vidarr within the hour, unmonitored.
2. Tag them `videos`, and check what Vidarr plans to download before tagging more artists.
3. Download a few albums from Soulseek, and confirm Lidarr labels their quality correctly. Tubifarry's
   docs don't explain how it detects quality.

To sync right away, run this in `/mnt/user/appdata/clippy-music`:

```bash
docker compose run --rm clippy-music-sync python /app/clippy_music.py sync
```

## Profiles

### Lidarr: Clippy Music

Lidarr grabs the highest rank it can find, then upgrades until it has lossless. Anything not listed is
disabled, including Unknown, WAV, APE, WavPack, WMA and OGG Vorbis.

| Rank | Group | Qualities |
| :-- | :-- | :-- |
| 1 | Lossless (cutoff) | FLAC 24bit, ALAC 24bit, FLAC, ALAC |
| 2 | 320 | MP3-320, AAC-320 |
| 3 | V0 | MP3-VBR-V0, AAC-VBR |
| 4 | 256 | MP3-256, AAC-256 |
| 5 | 192 | MP3-VBR-V2, MP3-192, AAC-192 |
| 6 | Low | MP3-160, MP3-128 |

### Vidarr

| Profile | Tag | Allowed | Cutoff |
| :-- | :-- | :-- | :-- |
| Clippy Music Video 1080p | `videos` | 1080p, 720p, YouTube, SD | 1080p |
| Clippy Music Video 4K | `videos-4k` | 2160p, 1080p, 720p, YouTube, SD | 2160p |

How Vidarr behaves, from its source code as of October 2026:

- **Unmonitored artists don't download automatically.** Its scheduled searches only cover monitored
  artists.
- **YouTube goes first when allowed.** It only searches indexers if YouTube has no confident match, or
  if the profile doesn't allow YouTube. For usenet and torrents only, remove `YouTube` from the
  allowed lists.
- **Upgrades only go up.** Vidarr ranks SD 1, YouTube and 720p 2, 1080p 3 and 2160p 4. A YouTube copy
  can later be replaced by a 1080p or 2160p release, but not by a 720p one.
- **Every YouTube download counts as rank 2**, whatever its real resolution. Vidarr may keep looking
  for a better copy of a video that is already 1080p or 4K.
- **Indexer releases are ranked by title.** A title with no resolution counts as SD.

### Changing a profile

Edit `profiles.json` in this repo and publish a new release. The Lidarr profile updates when the sync
restarts, and changes made directly in Lidarr are overwritten at that point. Vidarr's API can't edit a
profile, so the sync logs a warning and you make the same change on Vidarr's Quality Profiles page.

## Sync rules

- **Lidarr decides.** For any artist in Lidarr, changing monitoring or the profile in Vidarr is undone
  by the next sync. Use the tags instead.
- **Vidarr-only artists are left alone.**
- **Nothing gets deleted.** Removing an artist from Lidarr leaves them in Vidarr.
- **Soulseek is music only.** Vidarr doesn't support it.

## Publishing a release

Push a version tag such as `v1.0.1`. CI installs the stack on a Linux machine with real Lidarr, slskd
and Vidarr containers, tests the profiles and sync, and publishes the release only if that passes. It
doesn't cover Unraid itself, real downloads, Soulseek logins or the Tubifarry plugin.

## License

MIT. See [LICENSE](LICENSE). Lidarr, slskd and Vidarr are separate projects with their own licenses.
