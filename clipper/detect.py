"""Steps 3-5: score time windows, find spikes relative to the local baseline,
merge them into moments, rank them, and compute clip boundaries."""

from __future__ import annotations

import bisect
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field

from .chat import Message
from .config import ScoringConfig


@dataclass
class DetectParams:
    window: float = 10.0
    method: str = "ratio"          # "ratio" or "zscore"
    multiplier: float = 3.0
    z: float = 2.5
    baseline_span: float = 300.0   # seconds of surrounding chat used for the baseline
    min_score: float = 10.0
    merge_gap: float = 30.0
    pre_roll: float = 25.0
    post_roll: float = 10.0
    min_len: float = 15.0
    max_len: float = 60.0
    max_clips: int = 15
    rank_by: str = "score"         # "score" or "ratio"


@dataclass
class Window:
    start: float
    messages: int = 0              # all kept messages (uncapped)
    counted: int = 0               # messages counted after the per-user cap
    unique: int = 0
    keyword_bonus: float = 0.0
    score: float = 0.0
    baseline: float = 0.0
    threshold: float = 0.0
    z: float = 0.0
    muted: str | None = None
    spike: bool = False


@dataclass
class Moment:
    spike_start: float
    spike_end: float
    peak_time: float               # middle of the peak window
    peak_score: float
    baseline: float
    ratio: float
    z: float
    peak_unique: int
    clip_start: float = 0.0
    clip_end: float = 0.0
    messages: int = 0
    top_messages: list[tuple[str, int]] = field(default_factory=list)
    keyword_hits: dict[str, int] = field(default_factory=dict)


def score_windows(msgs: list[Message], length: float, scoring: ScoringConfig, p: DetectParams,
                  mutes: list[tuple[float, float, str]] = ()) -> list[Window]:
    n = max(1, math.ceil(max(length, msgs[-1].t + 1 if msgs else 0) / p.window))
    wins = [Window(start=i * p.window) for i in range(n)]
    per_user: list[Counter] = [Counter() for _ in range(n)]
    cap = scoring.per_user_cap if scoring.per_user_cap > 0 else math.inf

    for m in msgs:
        i = min(int(m.t // p.window), n - 1)
        w = wins[i]
        w.messages += 1
        per_user[i][m.user] += 1
        if per_user[i][m.user] > cap:
            continue
        kw, _ = scoring.keyword_weight(m.body)
        w.counted += 1
        w.keyword_bonus += kw

    for i, w in enumerate(wins):
        w.unique = len(per_user[i])
        w.score = window_score(w.counted, w.keyword_bonus, w.unique, scoring)
        for s, e, reason in mutes:
            if w.start < e and w.start + p.window > s:
                w.muted = reason
                break
    return wins


def window_score(counted: int, keyword_bonus: float, unique: int, scoring: ScoringConfig) -> float:
    return counted + keyword_bonus + scoring.unique_chatter_weight * unique


def baseline_threshold(score: float, neigh: list[float], p: DetectParams) -> tuple[float, float, float]:
    """(baseline, threshold, z) for a window score given its neighbouring window scores."""
    if not neigh:
        neigh = [0.0]
    mean = statistics.fmean(neigh)
    if p.method == "zscore":
        std = statistics.pstdev(neigh) if len(neigh) > 1 else 0.0
        std = max(std, 1.0, 0.1 * mean)  # avoid huge z on near-silent stretches
        return mean, max(mean + p.z * std, p.min_score), (score - mean) / std
    med = statistics.median(neigh)
    std = max(statistics.pstdev(neigh) if len(neigh) > 1 else 0.0, 1.0)
    return med, max(max(med, 1.0) * p.multiplier, p.min_score), (score - mean) / std


def find_spikes(wins: list[Window], p: DetectParams) -> None:
    """Mark windows whose score is high relative to the surrounding ~baseline_span seconds."""
    k = max(1, int(round(p.baseline_span / p.window / 2)))
    scores = [w.score for w in wins]
    for i, w in enumerate(wins):
        neigh = scores[max(0, i - k):i] + scores[i + 1:i + 1 + k]
        w.baseline, w.threshold, w.z = baseline_threshold(w.score, neigh, p)
        w.spike = w.muted is None and w.score >= w.threshold


def moment_from_group(group: list[Window], p: DetectParams) -> Moment:
    peak = max(group, key=lambda w: w.score)
    return Moment(
        spike_start=group[0].start,
        spike_end=group[-1].start + p.window,
        peak_time=peak.start + p.window / 2,
        peak_score=peak.score,
        baseline=peak.baseline,
        ratio=peak.score / max(peak.baseline, 1.0),
        z=peak.z,
        peak_unique=peak.unique,
    )


def merge_moments(wins: list[Window], p: DetectParams) -> list[Moment]:
    moments: list[Moment] = []
    group: list[Window] = []
    for w in wins:
        if not w.spike:
            continue
        if group and w.start - (group[-1].start + p.window) > p.merge_gap:
            moments.append(moment_from_group(group, p))
            group = []
        group.append(w)
    if group:
        moments.append(moment_from_group(group, p))
    return moments


def set_clip_bounds(m: Moment, length: float, p: DetectParams) -> None:
    start = m.spike_start - p.pre_roll
    end = m.spike_end + p.post_roll
    if end - start > p.max_len:
        # Long merged spike: anchor on the peak instead of the first spike window.
        start = max(start, m.peak_time - p.window / 2 - p.pre_roll)
        end = min(end, start + p.max_len)
    if end - start < p.min_len:
        end = start + p.min_len
    start, end = max(0.0, start), min(length, end) if length > 0 else end
    if end - start < p.min_len:  # hit a VOD edge: grow the other way
        if start == 0.0:
            end = min(length, p.min_len) if length > 0 else p.min_len
        else:
            start = max(0.0, end - p.min_len)
    m.clip_start, m.clip_end = round(start, 2), round(end, 2)


def _overlap(a: Moment, b: Moment) -> float:
    inter = min(a.clip_end, b.clip_end) - max(a.clip_start, b.clip_start)
    if inter <= 0:
        return 0.0
    shorter = min(a.clip_end - a.clip_start, b.clip_end - b.clip_start)
    return inter / shorter if shorter > 0 else 1.0


def summarize_chat(m: Moment, msgs: list[Message], scoring: ScoringConfig, top_n: int = 5) -> None:
    """Fill in the most common messages and keyword categories during the spike."""
    texts: Counter = Counter()
    first_form: dict[str, str] = {}
    cats: Counter = Counter()
    count = 0
    lo = m.spike_start
    hi = m.spike_end
    start = bisect.bisect_left(msgs, lo, key=lambda x: x.t)  # msgs are sorted by time
    for msg in msgs[start:]:
        if msg.t >= hi:
            break
        count += 1
        key = " ".join(msg.body.lower().split())
        first_form.setdefault(key, msg.body)
        texts[key] += 1
        _, cat = scoring.keyword_weight(msg.body)
        if cat:
            cats[cat] += 1
    m.messages = count
    m.top_messages = [(first_form[k], c) for k, c in texts.most_common(top_n)]
    m.keyword_hits = dict(cats.most_common())


def detect(msgs: list[Message], length: float, scoring: ScoringConfig, p: DetectParams,
           mutes: list[tuple[float, float, str]] = ()) -> tuple[list[Window], list[Moment]]:
    """Full detection. Returns all windows (for plotting) and the ranked, selected moments."""
    wins = score_windows(msgs, length, scoring, p, mutes)
    find_spikes(wins, p)
    moments = merge_moments(wins, p)
    for m in moments:
        set_clip_bounds(m, length, p)

    key = (lambda m: (m.ratio, m.peak_score)) if p.rank_by == "ratio" else (lambda m: (m.peak_score, m.ratio))
    moments.sort(key=key, reverse=True)

    selected: list[Moment] = []
    for m in moments:
        if len(selected) >= p.max_clips:
            break
        if any(_overlap(m, s) > 0.3 for s in selected):
            continue
        selected.append(m)

    for m in selected:
        summarize_chat(m, msgs, scoring)
    return wins, selected
