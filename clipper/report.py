"""Step 8: report.json / report.csv and console output."""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .chat import CleanStats, VodMeta
from .detect import DetectParams, Moment


def hms(t: float, compact: bool = True) -> str:
    t = int(max(0, t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h:02d}h{m:02d}m{s:02d}s" if compact else f"{h}:{m:02d}:{s:02d}"


def clip_filename(rank: int, m: Moment) -> str:
    return f"{rank:02d}_{hms(m.clip_start)}_score{int(round(m.peak_score))}.mp4"


def moment_record(rank: int, m: Moment, vod_id: str | None, file: str | None) -> dict:
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
        "vod_link": f"https://www.twitch.tv/videos/{vod_id}?t={hms(m.clip_start)}" if vod_id else None,
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
    _write_json(jpath, payload, indent=2)
    _write_csv(cpath, records)
    return jpath, cpath


def previous_clips(out_dir: Path) -> dict[str, dict]:
    """Clip records from an existing report.json, by file name ({} if there is none)."""
    try:
        data = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        return {c["file"]: c for c in data.get("clips") or [] if c.get("file")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_report_json(out_dir: Path, data: dict) -> None:
    """Rewrite report.json (and report.csv) as given, e.g. after captioning or deleting clips."""
    _write_json(out_dir / "report.json", data, indent=2)
    _write_csv(out_dir / "report.csv", data.get("clips") or [])


def _write_csv(cpath: Path, records: list[dict]) -> None:
    cols = ["rank", "file", "clipped_live", "clock_time", "clip_start_hms", "clip_start", "clip_end", "duration", "peak_time_hms",
            "peak_score", "baseline", "ratio", "peak_unique_chatters", "messages_in_spike",
            "top_messages", "vod_link"]
    # utf-8-sig so Excel shows emoji correctly
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["top_messages"] = " | ".join(f"{m['text']} (x{m['count']})" for m in r.get("top_messages") or [])
            w.writerow(row)


def window_row(w) -> list:
    """Compact per-window record for activity.json and live UI events:
    [start, score, threshold, spike, muted, messages]."""
    return [w.start, round(w.score, 1), round(w.threshold, 1), int(w.spike), w.muted or "", w.messages]


def write_activity(out_dir: Path, window: float, wins) -> Path:
    """Chat activity per window, for the web UI's chart."""
    path = out_dir / "activity.json"
    _write_json(path, {"window": window, "columns": ["start", "score", "threshold", "spike", "muted", "messages"],
                       "rows": [window_row(w) for w in wins]}, separators=(",", ":"))
    return path


def _write_json(path: Path, obj, **dump_kw) -> None:
    """Write to a temp file, then swap it in: the UI may read the file at any moment."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, **dump_kw)
    _replace(tmp, path)


def _replace(tmp: Path, dest: Path) -> None:
    """Atomic rename. On Windows it fails while another process has `dest` open, so retry."""
    for _ in range(20):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.replace(dest)


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
