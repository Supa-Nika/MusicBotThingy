"""Now-playing card: cover, text, progress, lyrics, spectrum visualizer, themed gradient, queue and votes.

Rendered with Pillow + numpy into a 960x540 PNG that the bot attaches to its 'now playing' message.
"""
from __future__ import annotations

import colorsys
import io
import os
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 960, 540
BARS = 56
COVER = 320
DEFAULT_COLORS = ((88, 101, 242), (24, 26, 48))

_REGULAR = [os.getenv("FONT_PATH", ""), "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
_BOLD = [os.getenv("FONT_PATH_BOLD", ""), "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False):
    for path in _BOLD if bold else _REGULAR:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1, scalable
    except TypeError:
        return ImageFont.load_default()


# --------------------------------------------------------------------------- theme
@dataclass
class Theme:
    bg: Image.Image                  # RGB, W x H gradient
    cover: Image.Image | None = None  # RGB, COVER x COVER
    mask: Image.Image | None = None   # rounded-corner mask for the cover


def _tone(rgb, vmin, vmax):
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    v = min(max(v, vmin), vmax)
    s = min(1.0, s * 1.15)
    return tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def _palette(cover: Image.Image):
    q = cover.resize((64, 64)).quantize(colors=6)
    pal = q.getpalette()
    cols = sorted(((n, tuple(pal[i * 3:i * 3 + 3])) for n, i in q.getcolors()), reverse=True)
    c1 = cols[0][1]
    rest = [c for _, c in cols[1:]] or [c1]
    c2 = max(rest, key=lambda c: sum((x - y) ** 2 for x, y in zip(c, c1)))
    return _tone(c1, 0.28, 0.65), _tone(c2, 0.10, 0.38)


def _gradient(c1, c2) -> Image.Image:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    t = np.clip(xx / W * 0.55 + yy / H * 0.75, 0, 1)[..., None]
    arr = np.array(c1, np.float32) * (1 - t) + np.array(c2, np.float32) * t
    d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
    arr *= (1 - 0.35 * np.clip(d - 0.4, 0, 1))[..., None]  # vignette
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB")


def build_theme(data: bytes | None) -> Theme:
    """Decode the cover (may be None) and derive the background gradient from its colours."""
    cover = mask = None
    c1, c2 = DEFAULT_COLORS
    if data:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            c1, c2 = _palette(img)
            cover = ImageOps.fit(img, (COVER, COVER), Image.LANCZOS)
            mask = Image.new("L", (COVER, COVER), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, COVER - 1, COVER - 1], radius=26, fill=255)
        except Exception:  # noqa: BLE001 - bad image: fall back to the default look
            cover = mask = None
            c1, c2 = DEFAULT_COLORS
    return Theme(_gradient(c1, c2), cover, mask)


# --------------------------------------------------------------------------- visualizer
def spectrum(pcm: bytes, n: int = BARS) -> np.ndarray:
    """Bar heights (0..1) from the most recent s16le stereo 48 kHz audio."""
    x = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<i2").astype(np.float32)
    if x.size < 4:
        return np.zeros(n)
    x = x.reshape(-1, 2).mean(axis=1) / 32768.0
    N = 2048
    x = np.pad(x, (N - x.size, 0)) if x.size < N else x[-N:]
    mag = np.abs(np.fft.rfft(x * np.hanning(N))) / (N / 4)
    freqs = np.fft.rfftfreq(N, 1 / 48000)
    centers = np.geomspace(45, 15000, n)
    vals = np.interp(centers, freqs, mag)
    db = 20 * np.log10(vals + 1e-7) + 3 * np.log2(centers / 100)  # tilt: highs are naturally quieter
    v = np.clip((db + 75) / 55, 0, 1)
    v = np.convolve(v, [0.25, 0.5, 0.25], mode="same")
    return v ** 1.2


# --------------------------------------------------------------------------- drawing helpers
def _fit(d: ImageDraw.ImageDraw, text: str, f, max_w: float) -> str:
    if d.textlength(text, font=f) <= max_w:
        return text
    while text and d.textlength(text + "...", font=f) > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


def _wrap(d: ImageDraw.ImageDraw, text: str, f, max_w: float, max_lines: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] += " ..."
    return [_fit(d, line, f, max_w) for line in lines] or [""]


def _text(d: ImageDraw.ImageDraw, xy, s: str, f, fill=(255, 255, 255, 255)) -> None:
    x, y = xy
    d.text((x + 2, y + 2), s, font=f, fill=(0, 0, 0, 120))  # soft shadow for readability
    d.text((x, y), s, font=f, fill=fill)


# --------------------------------------------------------------------------- card
@dataclass
class CardData:
    artist: str
    title: str
    elapsed: float
    duration: float | None
    elapsed_text: str
    total_text: str
    paused: bool = False
    pcm: bytes = b""
    upcoming: list[str] = field(default_factory=list)
    # (label, votes, needed, percent_now, percent_needed)
    votes: list[tuple[str, int, int, float, float]] = field(default_factory=list)
    lyrics: list[str] = field(default_factory=list)  # [line playing now, next, next, ...]
    lyrics_note: str = ""                            # shown instead of lines, e.g. "♪ Instrumental"


def render(theme: Theme, data: CardData) -> bytes:
    img = theme.bg.convert("RGBA")

    # background visualizer
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    bars = np.full(BARS, 0.03) if data.paused or not data.pcm else spectrum(data.pcm)
    slot = W / BARS
    bw = slot * 0.6
    for i, v in enumerate(bars):
        h = 8 + float(v) * H * 0.55
        x0 = i * slot + (slot - bw) / 2
        od.rounded_rectangle([x0, H - h, x0 + bw, H + bw], radius=bw / 2, fill=(255, 255, 255, 46))
    img = Image.alpha_composite(img, ov)

    # cover with shadow
    cx, cy = 48, 48
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([cx + 6, cy + 14, cx + COVER + 6, cy + COVER + 14], 28, fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(14)))
    if theme.cover is not None:
        img.paste(theme.cover.convert("RGBA"), (cx, cy), theme.mask)
    else:
        ImageDraw.Draw(img).rounded_rectangle([cx, cy, cx + COVER, cy + COVER], 26, fill=(255, 255, 255, 40))

    ui = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    u = ImageDraw.Draw(ui)
    x0, rw = 408, 504

    # with lyrics the right column gets compact: smaller title, progress bar higher, lyrics panel below
    compact = bool(data.lyrics or data.lyrics_note)
    if compact:
        label_y, artist_y, artist_size, title_y, title_size, title_lh = 38, 62, 24, 94, 34, 42
    else:
        label_y, artist_y, artist_size, title_y, title_size, title_lh = 54, 84, 28, 128, 44, 54

    _text(u, (x0, label_y), "PAUSED" if data.paused else "NOW PLAYING", font(16, True), (255, 255, 255, 170))
    _text(u, (x0, artist_y), _fit(u, data.artist or "Unknown artist", font(artist_size), rw), font(artist_size), (255, 255, 255, 230))
    tf = font(title_size, True)
    tlines = _wrap(u, data.title, tf, rw, 2)
    for i, line in enumerate(tlines):
        _text(u, (x0, title_y + i * title_lh), line, tf)

    # progress bar + time
    by = title_y + title_lh * len(tlines) + 12 if compact else 276
    u.rounded_rectangle([x0, by, x0 + rw, by + 8], 4, fill=(255, 255, 255, 70))
    if data.duration:
        frac = min(max(data.elapsed / data.duration, 0.0), 1.0)
        fx = x0 + max(rw * frac, 8)
        u.rounded_rectangle([x0, by, fx, by + 8], 4, fill=(255, 255, 255, 235))
        u.ellipse([fx - 9, by - 5, fx + 9, by + 13], fill=(255, 255, 255, 255))
    _text(u, (x0, by + (20 if compact else 26)), f"{data.elapsed_text} / {data.total_text}",
          font(20 if compact else 22, True), (255, 255, 255, 235))

    # lyrics panel: the line playing now (bold) and the next ones, fading out
    if compact:
        px0, py0, px1, py1 = x0 - 12, 246, x0 + rw, 382
        u.rounded_rectangle([px0, py0, px1, py1], 18, fill=(0, 0, 0, 95))
        tx, mw = px0 + 16, px1 - px0 - 32
        if data.lyrics:
            cf = font(24, True)
            for size in (24, 22, 20):  # shrink a long line before cutting it off
                cf = font(size, True)
                if u.textlength(data.lyrics[0], font=cf) <= mw:
                    break
            _text(u, (tx, py0 + 10), _fit(u, data.lyrics[0], cf, mw), cf)
            nf = font(19)
            for i, line in enumerate(data.lyrics[1:4]):
                _text(u, (tx, py0 + 47 + i * 27), _fit(u, line, nf, mw), nf, (255, 255, 255, (205, 165, 125)[i]))
        else:
            _text(u, (tx, py0 + 46), data.lyrics_note, font(22), (255, 255, 255, 190))

    # bottom panel: up next (+ votes)
    u.rounded_rectangle([48, 392, 912, 512], 22, fill=(0, 0, 0, 110))
    has_votes = bool(data.votes)
    list_w = 468 if has_votes else 816
    _text(u, (72, 402), "UP NEXT", font(14, True), (255, 255, 255, 160))
    lf = font(20)
    if data.upcoming:
        for i, label in enumerate(data.upcoming[:3]):
            _text(u, (72, 424 + i * 28), _fit(u, f"{i + 1}.  {label}", lf, list_w), lf, (255, 255, 255, 230))
    else:
        _text(u, (72, 424), "Nothing queued", lf, (255, 255, 255, 140))

    if has_votes:
        _text(u, (560, 402), "VOTES", font(14, True), (255, 255, 255, 160))
        nf, pf = font(18, True), font(16)
        for i, (label, votes, need, pct, req) in enumerate(data.votes[:3]):
            y = 424 + i * 28
            _text(u, (560, y), f"{label} {votes}/{need}", nf, (255, 255, 255, 235))
            u.rounded_rectangle([695, y + 9, 785, y + 17], 4, fill=(255, 255, 255, 70))
            fill_w = 90 * min(pct / req, 1.0) if req else 90
            if fill_w >= 1:
                u.rounded_rectangle([695, y + 9, 695 + max(fill_w, 8), y + 17], 4, fill=(255, 255, 255, 235))
            _text(u, (795, y + 2), f"{round(pct)}%/{round(req)}%", pf, (255, 255, 255, 200))

    out = Image.alpha_composite(img, ui).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()
