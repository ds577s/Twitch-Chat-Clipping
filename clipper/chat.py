"""Steps 1-2: download chat, load it, and clean out messages that cause fake spikes."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import ClipperError
from . import tools
from .config import FilterConfig


@dataclass
class Message:
    t: float          # seconds from VOD start
    user: str         # login name, lowercase
    body: str


@dataclass
class VodMeta:
    vod_id: str
    streamer: str | None = None
    title: str | None = None
    length: float | None = None   # seconds
    created_at: float | None = None  # stream start, unix time (used to match live clips)


@dataclass
class CleanStats:
    total: int = 0
    kept: int = 0
    dropped: Counter = field(default_factory=Counter)
    mutes: list[tuple[float, float, str]] = field(default_factory=list)  # (start, end, reason)


def parse_vod_id(s: str) -> str:
    s = s.strip()
    if s.isdigit():
        return s
    m = re.search(r"twitch\.tv/(?:videos|[^/]+/v(?:ideo)?)/(\d+)", s)
    if m:
        return m.group(1)
    raise ClipperError(f"Not a Twitch VOD URL or ID: {s!r} (expected e.g. https://www.twitch.tv/videos/123456789)")


def download_chat(vod_id: str, dest: Path, tdcli: str, oauth: str | None = None) -> None:
    """Download chat JSON with TwitchDownloaderCLI. Writes to a temp name then renames,
    so an interrupted download never leaves a half-written cache file."""
    tmp = dest.with_name(dest.stem + ".partial.json")
    tmp.unlink(missing_ok=True)
    cmd = [tdcli, "chatdownload", "--id", vod_id, "-o", str(tmp)]
    if oauth:
        cmd += ["--oauth", oauth]
    tools.run(cmd, f"Chat download for VOD {vod_id}")
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise ClipperError(f"Chat download for VOD {vod_id} produced no file. Is the VOD deleted or sub-only?")
    tmp.replace(dest)


def _parse_length(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", str(v))
    if m and any(m.groups()):
        h, mi, se = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + se
    try:
        return float(v)
    except ValueError:
        return None


def load_chat(path: Path, vod_id: str) -> tuple[VodMeta, list[dict]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ClipperError(f"Could not read chat file {path}: {e}\nDelete it and re-run to download again.") from e

    streamer = data.get("streamer") or {}
    video = data.get("video") or {}
    comments = data.get("comments") or []
    meta = VodMeta(
        vod_id=vod_id,
        streamer=(streamer.get("login") or streamer.get("name") or None),
        title=video.get("title"),
        length=_parse_length(video.get("length")),
    )
    if meta.streamer:
        meta.streamer = meta.streamer.lower()
    created = video.get("created_at")
    if created:
        try:
            meta.created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if meta.length is None or meta.length <= 0:
        # Older chat files: start/end are offsets of the downloaded range
        end = _parse_length(video.get("end"))
        if end:
            meta.length = end
        elif comments:
            meta.length = max(float(c.get("content_offset_seconds", 0)) for c in comments) + 1
        else:
            meta.length = 0.0
    return meta, comments


# Invisible characters chatters (and 7TV) append to dodge Twitch's duplicate-message
# filter, e.g. "?" + U+034F. Removed so both count as the same message.
_INVISIBLE_CODEPOINTS = (
    [0x00AD, 0x034F, 0x180E, 0x2800, 0xFEFF]   # soft hyphen, grapheme joiner, braille blank, BOM...
    + list(range(0x200B, 0x2010))              # zero-width space/joiners, direction marks
    + list(range(0x2060, 0x2065))              # word joiner, invisible operators
    + list(range(0xE0000, 0xE0080))            # tag characters
)
_INVISIBLE = re.compile("[" + "".join(map(chr, _INVISIBLE_CODEPOINTS)) + "]")


def normalize(body: str) -> str:
    return " ".join(_INVISIBLE.sub("", body).split())


def classify(user: str, body: str, bits: int, cfg: FilterConfig) -> str | None:
    """Why a (normalized) message should be dropped, or None to keep it.
    Shared by VOD mode and live mode."""
    if not body:
        return "empty"
    if user in cfg.bots:
        return "bot"
    if cfg.drop_commands and body.startswith("!"):
        return "command"
    if cfg.drop_bits and bits > 0:
        return "bits"
    if any(p.search(body) for p in cfg.system_patterns):
        return "system notice"
    if any(p.search(body) for p in cfg.prompted_patterns):
        return "poll vote"
    if any(p.search(body) for p in cfg.goodbye_patterns):
        return "goodbye"
    if cfg.raid_pattern and cfg.raid_pattern.search(body):
        return "raid message"
    return None


def clean(comments: list[dict], cfg: FilterConfig, window: float,
          length: float | None = None) -> tuple[list[Message], CleanStats]:
    """Drop bots, commands, cheers, system notices and poll votes; work out mute periods
    (raids, gift bombs, stream intro/outro)."""
    stats = CleanStats(total=len(comments))
    if cfg.ignore_first_seconds > 0:
        stats.mutes.append((0.0, cfg.ignore_first_seconds, "stream intro"))
    if length and cfg.ignore_last_seconds > 0:
        stats.mutes.append((max(0.0, length - cfg.ignore_last_seconds), length, "stream outro"))
    kept: list[Message] = []
    raid_hits: Counter = Counter()  # window index -> count of raid-ish messages

    for c in comments:
        commenter = c.get("commenter") or {}
        msg = c.get("message") or {}
        user = (commenter.get("name") or commenter.get("login") or commenter.get("display_name") or "").lower()
        body = normalize(msg.get("body") or "")
        t = float(c.get("content_offset_seconds", 0) or 0)

        if body and cfg.gift_pattern:
            m = cfg.gift_pattern.search(body)
            if m and int(m.group(1)) >= cfg.gift_min_count:
                stats.mutes.append((t, t + cfg.gift_mute_seconds, f"gift bomb ({m.group(1)} subs)"))
        if body and cfg.raid_notice_pattern and cfg.raid_notice_pattern.search(body):
            stats.mutes.append((t, t + cfg.raid_mute_seconds, "raid"))

        reason = classify(user, body, msg.get("bits_spent") or 0, cfg)
        if reason == "raid message":
            raid_hits[int(t // window)] += 1
        if reason:
            stats.dropped[reason] += 1
            continue
        kept.append(Message(t=t, user=user or "?", body=body))

    for w, n in sorted(raid_hits.items()):
        if n >= cfg.raid_min_messages:
            start = w * window
            stats.mutes.append((start, start + window + cfg.raid_mute_seconds, f"raid ({n} raid messages)"))

    stats.mutes = _merge_intervals(stats.mutes)
    kept.sort(key=lambda m: m.t)
    stats.kept = len(kept)
    return kept, stats


def _merge_intervals(iv: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    for s, e, r in sorted(iv):
        if out and s <= out[-1][1]:
            ps, pe, pr = out[-1]
            kinds = {x.split(" (")[0] for x in pr.split("; ")}
            out[-1] = (ps, max(pe, e), pr if r.split(" (")[0] in kinds else f"{pr}; {r}")
        else:
            out.append((s, e, r))
    return out
