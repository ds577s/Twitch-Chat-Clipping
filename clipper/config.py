"""Loading the YAML config files (keywords, filters, streamer facecams)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import ClipperError

DEFAULT_DIR = Path(__file__).parent / "config"


def _load_yaml(path: Path | None, default_name: str) -> dict:
    p = Path(path) if path else DEFAULT_DIR / default_name
    if not p.exists():
        raise ClipperError(f"Config file not found: {p}")
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ClipperError(f"Could not parse {p}:\n{e}") from e


def _compile_keyword(kw: str) -> re.Pattern:
    kw = str(kw)
    if kw.startswith("re:"):
        return re.compile(kw[3:], re.IGNORECASE)
    if re.fullmatch(r"[\w ']+", kw):
        # whole-word match: "W" must not match inside "WOW"
        return re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE)
    # emoji / symbols: plain substring
    return re.compile(re.escape(kw))


@dataclass
class KeywordCategory:
    name: str
    weight: float
    patterns: list[re.Pattern]


@dataclass
class ScoringConfig:
    per_user_cap: int = 1
    unique_chatter_weight: float = 1.0
    categories: list[KeywordCategory] = field(default_factory=list)

    def keyword_weight(self, body: str) -> tuple[float, str | None]:
        """Highest keyword weight found in a message, and its category."""
        best, cat = 0.0, None
        for c in self.categories:
            if c.weight > best and any(p.search(body) for p in c.patterns):
                best, cat = c.weight, c.name
        return best, cat


@dataclass
class FilterConfig:
    bots: set[str]
    drop_commands: bool
    drop_bits: bool
    system_patterns: list[re.Pattern]
    prompted_patterns: list[re.Pattern]
    ignore_first_seconds: float
    ignore_last_seconds: float
    raid_pattern: re.Pattern | None
    raid_min_messages: int
    raid_mute_seconds: float
    gift_pattern: re.Pattern | None
    gift_min_count: int
    gift_mute_seconds: float


def load_scoring(path: Path | None = None) -> ScoringConfig:
    data = _load_yaml(path, "keywords.yaml")
    s = data.get("scoring", {}) or {}
    cats = []
    for name, c in (data.get("categories") or {}).items():
        cats.append(KeywordCategory(
            name=name,
            weight=float(c.get("weight", 1.0)),
            patterns=[_compile_keyword(k) for k in c.get("keywords", [])],
        ))
    return ScoringConfig(
        per_user_cap=int(s.get("per_user_cap", 1)),
        unique_chatter_weight=float(s.get("unique_chatter_weight", 1.0)),
        categories=cats,
    )


def load_filters(path: Path | None = None) -> FilterConfig:
    d = _load_yaml(path, "filters.yaml")
    raid = d.get("raid") or {}
    gift = d.get("gift_bomb") or {}
    return FilterConfig(
        bots={b.lower() for b in d.get("bots", [])},
        drop_commands=bool(d.get("drop_commands", True)),
        drop_bits=bool(d.get("drop_bits", True)),
        system_patterns=[re.compile(p, re.IGNORECASE) for p in d.get("system_patterns", [])],
        prompted_patterns=[re.compile(p, re.IGNORECASE) for p in d.get("prompted_patterns", []) or []],
        ignore_first_seconds=float(d.get("ignore_first_seconds", 0) or 0),
        ignore_last_seconds=float(d.get("ignore_last_seconds", 0) or 0),
        raid_pattern=re.compile(raid["pattern"], re.IGNORECASE) if raid.get("pattern") else None,
        raid_min_messages=int(raid.get("min_messages", 5)),
        raid_mute_seconds=float(raid.get("mute_seconds", 90)),
        gift_pattern=re.compile(gift["pattern"], re.IGNORECASE) if gift.get("pattern") else None,
        gift_min_count=int(gift.get("min_count", 5)),
        gift_mute_seconds=float(gift.get("mute_seconds", 45)),
    )


def load_facecams(streamer: str | None, path: Path | None = None) -> list[dict]:
    """Candidate facecam boxes [{x, y, w, h, ref_height}] for a streamer (may be empty).

    Accepts either `facecam: {...}` (one position) or `facecams: [{...}, ...]`
    (streamer moves the cam between scenes; the right one is picked per clip)."""
    if not streamer:
        return []
    data = _load_yaml(path, "streamers.yaml")
    entry = (data.get("streamers") or {}).get(streamer.lower())
    if not entry:
        return []
    raw = entry.get("facecams") or ([entry["facecam"]] if entry.get("facecam") else [])
    boxes = []
    for fc in raw:
        try:
            box = {k: float(fc[k]) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError) as e:
            raise ClipperError(f"streamers.yaml: each facecam for '{streamer}' needs numeric x, y, w, h") from e
        box["ref_height"] = float(fc.get("ref_height", entry.get("ref_height", 1080)))
        boxes.append(box)
    return boxes
