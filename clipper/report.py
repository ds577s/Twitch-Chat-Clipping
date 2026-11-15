"""Step 8: report.json / report.csv and console output."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .chat import CleanStats, VodMeta
from .detect import DetectParams, Moment


def hms(t: float, compact: bool = True) -> str:
    t = int(max(0, t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h:02d}h{m:02d}m{s:02d}s" if compact else f"{h}:{m:02d}:{s:02d}"


def clip_filename(rank: int, m: Moment) -> str:
    return f"{rank:02d}_{hms(m.clip_start)}_score{int(round(m.peak_score))}.mp4"


def moment_record(rank: int, m: Moment, vod_id: str, file: str | None) -> dict:
    return {
        "rank": rank,
        "file": file,
        "clip_start": m.clip_start,
        "clip_end": m.clip_end,
        "duration": round(m.clip_end - m.clip_start, 2),
        "clip_start_hms": hms(m.clip_start, compact=False),
        "spike_start": m.spike_start,
        "spike_end": m.spike_end,
        "peak_time": m.peak_time,
        "peak_time_hms": hms(m.peak_time, compact=False),
        "peak_score": round(m.peak_score, 1),
        "baseline": round(m.baseline, 1),
        "ratio": round(m.ratio, 2),
        "z": round(m.z, 2),
        "peak_unique_chatters": m.peak_unique,
        "messages_in_spike": m.messages,
        "keyword_hits": m.keyword_hits,
        "top_messages": [{"text": t, "count": c} for t, c in m.top_messages],
        "vod_link": f"https://www.twitch.tv/videos/{vod_id}?t={hms(m.clip_start)}",
    }


def write_reports(out_dir: Path, meta: VodMeta, params: DetectParams, stats: CleanStats,
                  records: list[dict]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / "report.json"
    cpath = out_dir / "report.csv"
    payload = {
        "vod_id": meta.vod_id,
        "streamer": meta.streamer,
        "title": meta.title,
        "vod_length": meta.length,
        "chat": {
            "total_messages": stats.total,
            "kept_messages": stats.kept,
            "dropped": dict(stats.dropped),
            "muted_periods": [{"start": s, "end": e, "reason": r} for s, e, r in stats.mutes],
        },
        "params": vars(params),
        "clips": records,
    }
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    cols = ["rank", "file", "clip_start_hms", "clip_start", "clip_end", "duration", "peak_time_hms",
            "peak_score", "baseline", "ratio", "peak_unique_chatters", "messages_in_spike",
            "top_messages", "vod_link"]
    # utf-8-sig so Excel shows emoji correctly
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["top_messages"] = " | ".join(f"{m['text']} (x{m['count']})" for m in r["top_messages"])
            w.writerow(row)
    return jpath, cpath


def print_summary(meta: VodMeta, stats: CleanStats, records: list[dict]) -> None:
    print(f"\nVOD {meta.vod_id}" + (f" — {meta.streamer}" if meta.streamer else "")
          + (f" — {meta.title}" if meta.title else ""))
    print(f"Length {hms(meta.length or 0, compact=False)} | chat: {stats.total} messages, "
          f"{stats.kept} kept after cleaning")
    if stats.dropped:
        print("  dropped: " + ", ".join(f"{k} {v}" for k, v in stats.dropped.most_common()))
    for s, e, r in stats.mutes:
        print(f"  muted {hms(s, False)}–{hms(e, False)}: {r}")
    if not records:
        print("\nNo spikes found. Try a lower --multiplier or --min-score, or check the chat file.")
        return
    print(f"\n{'#':>2}  {'clip start':>10}  {'len':>4}  {'score':>6}  {'base':>5}  {'x':>5}  top chat")
    for r in records:
        top = ", ".join(f"{m['text'][:25]}" for m in r["top_messages"][:3])
        print(f"{r['rank']:>2}  {r['clip_start_hms']:>10}  {r['duration']:>4.0f}  {r['peak_score']:>6.1f}  "
              f"{r['baseline']:>5.1f}  {r['ratio']:>5.1f}  {top}")
