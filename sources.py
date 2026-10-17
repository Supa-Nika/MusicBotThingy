"""Link detection and track loading.

Spotify  -> metadata only (title/artist) read from Spotify's public embed page;
            the audio itself is found on YouTube at play time.
YouTube / YouTube Music -> yt-dlp.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from itertools import zip_longest
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

import aiohttp
import yt_dlp

log = logging.getLogger("musicbot.sources")

MAX_TRACKS = int(os.getenv("MAX_PLAYLIST_TRACKS", "10000"))
STREAM_TTL = 3 * 3600  # YouTube stream URLs expire; re-resolve after this
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]+/)?|spotify:)(playlist|album|track)[/:]([A-Za-z0-9]{22})"
)
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
DEAD_ENTRIES = {"[Private video]", "[Deleted video]"}


class SourceError(Exception):
    """An error whose message is safe to show to users."""


def short(err: Exception) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()
    return (text.splitlines()[0] if text else type(err).__name__)[:150]


def ydl_opts(**extra) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if os.getenv("YTDLP_COOKIES"):  # only needed for age-restricted / members-only videos
        opts["cookiefile"] = os.environ["YTDLP_COOKIES"]
    opts.update(extra)
    return opts


# --------------------------------------------------------------------------- tracks
class Track:
    def __init__(self, title, artist="", url=None, query=None, duration=None):
        self.title = title
        self.artist = artist
        self.url = url            # YouTube watch URL (known up front for YT sources)
        self.query = query        # "artist - title" (Spotify sources: searched on YouTube lazily)
        self.duration = duration  # seconds, used to pick the right YouTube match
        self.stream_url = None
        self.thumbnail = None     # cover image URL, filled in by resolve()
        self._resolved_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def label(self) -> str:
        text = f"{self.artist} – {self.title}" if self.artist else self.title
        return text if len(text) <= 90 else text[:87] + "..."

    async def resolve(self) -> None:
        """Find the YouTube video (if needed) and a fresh direct audio URL."""
        async with self._lock:
            if self.stream_url and time.time() - self._resolved_at < STREAM_TTL:
                return
            await asyncio.to_thread(self._resolve_blocking)

    def _resolve_blocking(self) -> None:
        if not self.url:
            self.url = _search_youtube(self.query, self.duration)
        with yt_dlp.YoutubeDL(ydl_opts(format="bestaudio/best", noplaylist=True)) as ydl:
            info = ydl.extract_info(self.url, download=False)
        self.stream_url = info["url"]
        self.thumbnail = info.get("thumbnail") or self.thumbnail
        self._resolved_at = time.time()
        if not self.artist:  # a pasted single video: fill in real metadata
            self.title = info.get("track") or info.get("title") or self.title
            self.artist = info.get("artist") or info.get("uploader") or ""
        self.duration = info.get("duration") or self.duration


def _search_youtube(query: str, want_duration: float | None) -> str:
    with yt_dlp.YoutubeDL(ydl_opts(extract_flat=True)) as ydl:
        res = ydl.extract_info(f"ytsearch5:{query}", download=False)
    hits = [e for e in (res or {}).get("entries", []) if e and e.get("id")]
    if not hits:
        raise SourceError(f"no YouTube match for '{query}'")
    best = hits[0]
    if want_duration:  # prefer the result whose length matches the Spotify track
        timed = [e for e in hits if e.get("duration")]
        if timed:
            best = min(timed, key=lambda e: abs(e["duration"] - want_duration))
    return f"https://www.youtube.com/watch?v={best['id']}"


# --------------------------------------------------------------------------- link analysis
def classify(link: str):
    """-> ("spotify", kind, id) or ("youtube", "playlist" | "video", normalised_url)"""
    m = SPOTIFY_RE.search(link)
    if m:
        return "spotify", m.group(1), m.group(2)
    u = urlparse(link)
    host = u.netloc.lower()
    if host in YT_HOSTS:
        qs = parse_qs(u.query)
        pl = qs.get("list", [""])[0]
        # "RD..." ids are auto-generated radio mixes, not real playlists -> treat as a single video
        if pl and not pl.startswith("RD"):
            return "youtube", "playlist", f"https://www.youtube.com/playlist?list={pl}"
        vid = u.path.lstrip("/").split("/")[0] if host == "youtu.be" else qs.get("v", [""])[0]
        if vid:
            return "youtube", "video", f"https://www.youtube.com/watch?v={vid}"
    raise SourceError("I only understand Spotify, YouTube and YouTube Music links.")


class Loaded(NamedTuple):
    tracks: list
    name: str
    shuffle: bool
    note: str = ""


async def load(link: str) -> Loaded:
    kind = classify(link)
    if kind[0] == "spotify":
        _, what, sid = kind
        tracks, name, truncated = await _spotify(what, sid)
        note = ""
        if truncated:
            note = "ℹ️ I could only read the first 100 tracks of that playlist - if yours is longer, the rest is missing."
        return Loaded(tracks, f"Spotify {what} **{name}**", what == "playlist", note)

    _, what, url = kind
    if what == "playlist":
        try:
            tracks, name = await asyncio.to_thread(_youtube_playlist, url)
        except yt_dlp.utils.DownloadError as e:
            raise SourceError(f"Couldn't read that playlist ({short(e)})") from e
        return Loaded(tracks, f"YouTube playlist **{name}**", True)

    track = Track(title=url, url=url)
    try:
        await track.resolve()
    except Exception as e:  # noqa: BLE001 - surface any yt-dlp failure to the user
        raise SourceError(f"Couldn't load that video ({short(e)})") from e
    return Loaded([track], "YouTube video", False)


def _youtube_playlist(url: str):
    with yt_dlp.YoutubeDL(ydl_opts(extract_flat="in_playlist", playlistend=MAX_TRACKS)) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = []
    for e in info.get("entries") or []:
        if not e or not e.get("id") or e.get("title") in DEAD_ENTRIES:
            continue
        channel = (e.get("channel") or e.get("uploader") or "").removesuffix(" - Topic")
        tracks.append(
            Track(
                e.get("title") or e["id"],
                channel,
                url=f"https://www.youtube.com/watch?v={e['id']}",
                duration=e.get("duration"),
            )
        )
    if not tracks:
        raise SourceError("That playlist is empty or private.")
    return tracks, info.get("title") or "playlist"


# --------------------------------------------------------------------------- Spotify (embed page)
async def _spotify_full_playlist(sid: str):
    """Whole playlist via SpotifyScraper (anonymous, no API key)."""
    from spotify_scraper import AsyncSpotifyClient

    async with AsyncSpotifyClient() as client:
        pl = await client.get_playlist(sid, max_tracks=MAX_TRACKS)
    tracks = []
    for item in pl.tracks or ():
        t = getattr(item, "track", item)  # items are PlaylistTrack wrappers
        title = getattr(t, "name", None)
        if not title or getattr(t, "playable", True) is False:
            continue
        artist = ", ".join(a.name for a in (t.artists or ()) if a.name)
        ms = t.duration_ms or 0
        tracks.append(
            Track(title, artist, query=f"{artist} - {title}".strip(" -"), duration=ms / 1000 or None)
        )
    if not tracks:
        raise SourceError("scraper returned no tracks")
    return tracks, pl.name or "Spotify"


async def _spotify(kind: str, sid: str):
    """-> (tracks, name, truncated)"""
    if kind == "playlist":
        try:
            tracks, name = await _spotify_full_playlist(sid)
            log.info("SpotifyScraper: loaded %d tracks from '%s'", len(tracks), name)
            return tracks, name, False
        except Exception as e:  # noqa: BLE001 - any failure -> fall back to the embed page
            log.warning("SpotifyScraper failed (%s) - falling back to embed page (max 100 tracks)", short(e))

    url = f"https://open.spotify.com/embed/{kind}/{sid}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    raise SourceError(f"Spotify answered HTTP {r.status} for that link.")
                html = await r.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise SourceError("Couldn't reach Spotify.") from e
    tracks, name = parse_spotify_embed(html)
    return tracks, name, kind == "playlist" and len(tracks) >= 100


def parse_spotify_embed(html: str):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    entity = None
    if m:
        try:
            entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        except (KeyError, TypeError, ValueError):
            entity = None
    if not entity:
        raise SourceError(
            "Couldn't read that Spotify link (private, removed, or Spotify changed its page layout)."
        )
    items = entity["trackList"] if "trackList" in entity else [entity]
    tracks = []
    for it in items:
        title = it.get("title") or it.get("name")
        if not title or it.get("isPlayable") is False:
            continue
        artist = it.get("subtitle") or ", ".join(a.get("name", "") for a in it.get("artists") or [])
        artist = artist.replace("\xa0", " ").strip()
        ms = it.get("duration") or 0
        tracks.append(
            Track(title, artist, query=f"{artist} - {title}".strip(" -"), duration=ms / 1000 or None)
        )
    if not tracks:
        raise SourceError("That Spotify link has no playable tracks.")
    return tracks, entity.get("name") or entity.get("title") or "Spotify"


# --------------------------------------------------------------------------- queue mixing
_GAP = object()


def interleave(old: list, new: list, spread: bool = False) -> list:
    """Mix `new` into `old`. New tracks come first.

    alternate: n1 o1 n2 o2 ... then whatever is left of the longer list
    spread:    new tracks are spread evenly across the whole queue
    """
    if spread:
        keyed = [((i + 0.5) / len(new), 0, t) for i, t in enumerate(new)]
        keyed += [((i + 0.5) / len(old), 1, t) for i, t in enumerate(old)]
        keyed.sort(key=lambda k: k[:2])
        return [t for *_, t in keyed]
    out = []
    for pair in zip_longest(new, old, fillvalue=_GAP):
        out.extend(t for t in pair if t is not _GAP)
    return out