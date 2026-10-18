"""Time-synced lyrics from LRCLIB (https://lrclib.net) - free, no API key.

fetch(track)  fills track.lyrics = [(seconds, text), ...] (empty list = none found)
              and track.instrumental. On a network error nothing is cached, so the next play retries.
window(...)   picks the line that is playing now plus the next few, for the now-playing card.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import os
import re

import aiohttp

log = logging.getLogger("musicbot.lyrics")

API = "https://lrclib.net/api"
UA = "musicbot (private discord bot)"  # LRCLIB asks clients to identify themselves
TIMEOUT = aiohttp.ClientTimeout(total=6)
# LRCLIB answers /api/get only for a duration within +-2 s; /api/search results are filtered with this.
TOLERANCE = float(os.getenv("LYRICS_DURATION_TOLERANCE", "6"))
# If the YouTube stream is this much longer/shorter than the Spotify track it is probably a different
# cut (music video intro...), so the timestamps would be off: show nothing rather than wrong lines.
MAX_DRIFT = float(os.getenv("LYRICS_MAX_DRIFT", "8"))

_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced-LRC per-word stamps
_BRACKETS = re.compile(r"\(.*?\)|\[.*?\]")
_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$", re.I)


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """LRC text -> sorted [(seconds, line)]. A line may carry several timestamps (repeated chorus)."""
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = list(_STAMP.finditer(raw))
        if not stamps:  # metadata tags like [ar:Artist] or blank lines
            continue
        line = _WORD_TAG.sub("", raw[stamps[-1].end():]).strip()
        for m in stamps:
            out.append((int(m.group(1)) * 60 + float(m.group(2).replace(":", ".")), line))
    out.sort(key=lambda x: x[0])
    return out


def window(lines: list[tuple[float, str]], t: float, ahead: int = 3) -> list[str]:
    """[line playing at time t, next line, ...] with up to `ahead` upcoming non-empty lines.

    The first entry is "♪" during instrumental breaks and before the first line.
    """
    if not lines:
        return []
    i = bisect.bisect_right([ts for ts, _ in lines], t) - 1
    out = [(lines[i][1] if i >= 0 else "") or "♪"]
    for _, text in lines[i + 1:]:
        if text:
            out.append(text)
            if len(out) > ahead:
                break
    return out


# --------------------------------------------------------------------------- lookup
def _clean(s: str) -> str:
    s = _FEAT.sub("", _BRACKETS.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip(" -\u2013\u2014")


def candidates(track) -> list[tuple[str, str]]:
    """(artist, title) pairs to try, best first."""
    title, artist = track.title or "", track.artist or ""
    out: list[tuple[str, str]] = []
    if track.query:  # Spotify: clean metadata, only drop "- Remastered 2011" style suffixes
        first = artist.split(",")[0].strip()
        out.append((first, _clean(title.split(" - ", 1)[0])))
        out.append((first, _clean(title)))
    else:  # YouTube: usually "Artist - Song (Official Video)" on a channel called "Artist - Topic"
        if " - " in title:
            a, t = title.split(" - ", 1)
            out.append((_clean(a), _clean(t)))
        out.append((_clean(artist.removesuffix(" - Topic")), _clean(title)))
    seen, uniq = set(), []
    for pair in out:
        if all(pair) and pair not in seen:
            seen.add(pair)
            uniq.append(pair)
    return uniq


async def _get(s: aiohttp.ClientSession, path: str, params: dict):
    async with s.get(f"{API}{path}", params=params) as r:
        if r.status >= 500:
            r.raise_for_status()  # outage: raise so the miss is not cached
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def _lookup(s: aiohttp.ClientSession, artist: str, title: str, duration: float | None):
    params = {"artist_name": artist, "track_name": title}
    if duration:
        params["duration"] = int(round(duration))
    rec = await _get(s, "/get", params)
    if isinstance(rec, dict) and (rec.get("syncedLyrics") or rec.get("instrumental")):
        return rec
    results = await _get(s, "/search", {"artist_name": artist, "track_name": title})
    best = None
    for r in results if isinstance(results, list) else []:
        if not (r.get("syncedLyrics") or r.get("instrumental")):
            continue
        d = r.get("duration")
        diff = abs(d - duration) if (duration and d) else 0.0
        if duration and d and diff > TOLERANCE:
            continue
        if best is None or diff < best[0]:
            best = (diff, r)
    return best[1] if best else None


async def fetch(track) -> None:
    if track.lyrics is not None:
        return
    # the Spotify length is the best reference for matching; the stream length only for the drift check
    ref = track.meta_duration or track.duration
    if track.meta_duration and track.duration and abs(track.meta_duration - track.duration) > MAX_DRIFT:
        log.info("lyrics skipped for %s: stream is %.0fs, track is %.0fs", track.label, track.duration, track.meta_duration)
        track.lyrics = []
        return
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=TIMEOUT) as s:
            for artist, title in candidates(track):
                rec = await _lookup(s, artist, title, ref)
                if not rec:
                    continue
                if rec.get("instrumental"):
                    track.instrumental, track.lyrics = True, []
                    return
                lines = parse_lrc(rec.get("syncedLyrics") or "")
                if lines:
                    track.lyrics = lines
                    log.info("lyrics found for %s (%d lines)", track.label, len(lines))
                    return
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.info("lyrics lookup failed for %s: %s", track.label, e)
        return  # not cached: try again next time
    track.lyrics = []
    log.info("no synced lyrics for %s", track.label)
