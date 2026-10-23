"""Time-synced lyrics from several free sources, no API keys: LRCLIB, NetEase, Kugou.

Sources are queried in parallel and the first one in LYRICS_SOURCES order that has synced lyrics wins.

fetch(track)  fills track.lyrics = [(seconds, text), ...] (empty list = none found)
              and track.instrumental. On a network error nothing is cached, so the next play retries.
window(...)   picks the line that is playing now plus the next few, for the now-playing card.
"""
from __future__ import annotations

import asyncio
import base64
import bisect
import logging
import os
import re

import aiohttp

from discordlog import request

log = logging.getLogger("tardbot.music.lyrics")

API = "https://lrclib.net/api"
UA = "tardbot (private discord bot)"  # LRCLIB asks clients to identify themselves
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


# --------------------------------------------------------------------------- sources
# Every source is `async (session, candidates, ref_duration) -> (lines, instrumental) | None`
# and raises aiohttp errors / TimeoutError on a network problem (so the miss is not cached).
Result = tuple[list[tuple[float, str]], bool]

_CREDITS = re.compile(
    r"^\s*(?:\u4f5c\u8bcd|\u4f5c\u66f2|\u7f16\u66f2|\u5236\u4f5c\u4eba|\u8bcd|\u66f2|lyrics?(?: by)?|music(?: by)?|"
    r"composer|arranger|producer|written by)\s*[:\uff1a]", re.I)


def _strip_credits(lines: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """NetEase/Kugou put 'Lyrics by: ...' credit rows at the start of the file."""
    head = 0
    while head < min(len(lines), 8) and (_CREDITS.match(lines[head][1]) or not lines[head][1] and head == 0):
        head += 1
    return lines[head:]


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", s.casefold())


def _artist_match(wanted: str, got: str) -> bool:
    a, g = _norm(wanted), _norm(got)
    return bool(a and g and (a in g or g in a))


async def _json(s: aiohttp.ClientSession, url: str, params: dict, headers: dict | None = None):
    async with s.get(url, params=params, headers=headers) as r:
        if r.status >= 500:
            r.raise_for_status()
        if r.status != 200:
            return None
        return await r.json(content_type=None)


async def _lrclib(s, cands, ref) -> Result | None:
    for artist, title in cands:
        rec = await _lookup(s, artist, title, ref)
        if not rec:
            continue
        if rec.get("instrumental"):
            return [], True
        lines = parse_lrc(rec.get("syncedLyrics") or "")
        if lines:
            return lines, False
    return None


NETEASE = "https://music.163.com/api"
_NE_HEADERS = {"Referer": "https://music.163.com/", "Cookie": "appver=2.0.2"}


async def _netease(s, cands, ref) -> Result | None:
    for artist, title in cands:
        data = await _json(s, f"{NETEASE}/search/get", {"s": f"{artist} {title}", "type": 1, "limit": 8}, _NE_HEADERS)
        songs = ((data or {}).get("result") or {}).get("songs") or []
        ranked = []
        for song in songs:
            dur = (song.get("duration") or song.get("dt") or 0) / 1000
            diff = abs(dur - ref) if (ref and dur) else 0.0
            if ref and dur and diff > TOLERANCE:
                continue
            by_artist = any(_artist_match(artist, a.get("name", "")) for a in song.get("artists") or [])
            if by_artist or diff <= 2:
                ranked.append((not by_artist, diff, song["id"]))
        for _, _, sid in sorted(ranked)[:3]:
            lyr = await _json(s, f"{NETEASE}/song/lyric", {"id": sid, "lv": 1}, _NE_HEADERS)
            lines = _strip_credits(parse_lrc(((lyr or {}).get("lrc") or {}).get("lyric") or ""))
            if lines:
                return lines, False
    return None


async def _kugou(s, cands, ref) -> Result | None:
    for artist, title in cands:
        kw = f"{artist} {title}"
        found = await _json(s, "https://mobileservice.kugou.com/api/v3/search/song",
                            {"format": "json", "keyword": kw, "page": 1, "pagesize": 6})
        infos = ((found or {}).get("data") or {}).get("info") or []
        for info in infos[:4]:
            dur = info.get("duration") or 0
            if ref and dur and abs(dur - ref) > TOLERANCE:
                continue
            if not _artist_match(artist, info.get("singername", "")):
                continue
            cs = await _json(s, "https://lyrics.kugou.com/search",
                             {"ver": 1, "man": "yes", "client": "pc", "keyword": kw,
                              "duration": int(dur * 1000), "hash": info.get("hash", "")})
            for c in ((cs or {}).get("candidates") or [])[:2]:
                dl = await _json(s, "https://lyrics.kugou.com/download",
                                 {"ver": 1, "client": "pc", "id": c["id"], "accesskey": c["accesskey"],
                                  "fmt": "lrc", "charset": "utf8"})
                raw = (dl or {}).get("content")
                if not raw:
                    continue
                lines = _strip_credits(parse_lrc(base64.b64decode(raw).decode("utf-8-sig", "replace")))
                if lines:
                    return lines, False
    return None


SOURCES = {"lrclib": _lrclib, "netease": _netease, "kugou": _kugou}
ENABLED = [n.strip() for n in os.getenv("LYRICS_SOURCES", "lrclib,netease,kugou").split(",") if n.strip() in SOURCES]
SOURCE_TIMEOUT = float(os.getenv("LYRICS_SOURCE_TIMEOUT", "10"))


async def fetch(track) -> None:
    if track.lyrics is not None:
        return
    # the Spotify length is the best reference for matching; the stream length only for the drift check
    ref = track.meta_duration or track.duration
    if track.meta_duration and track.duration and abs(track.meta_duration - track.duration) > MAX_DRIFT:
        request(log, "lyrics skipped for %s: stream is %.0fs, track is %.0fs", track.label, track.duration, track.meta_duration)
        track.lyrics = []
        return
    cands = candidates(track)
    request(log, "lyrics: looking up %s", track.label)
    failed = False
    async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=TIMEOUT) as s:
        # all sources run in parallel; results are taken in priority order
        tasks = [(name, asyncio.create_task(asyncio.wait_for(SOURCES[name](s, cands, ref), SOURCE_TIMEOUT)))
                 for name in ENABLED]
        try:
            for name, task in tasks:
                try:
                    res = await task
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as e:
                    log.warning("lyrics source %s failed for %s: %s %s", name, track.label, type(e).__name__, e)
                    failed = True
                    continue
                if not res:
                    continue
                lines, instrumental = res
                if instrumental:
                    track.instrumental, track.lyrics = True, []
                else:
                    track.lyrics = lines
                    request(log, "lyrics found for %s via %s (%d lines)", track.label, name, len(lines))
                return
        finally:
            for _, task in tasks:
                task.cancel()
    if failed:
        return  # not cached: try again next time
    track.lyrics = []
    request(log, "no synced lyrics for %s", track.label)