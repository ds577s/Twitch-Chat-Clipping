"""--plot: PNG of chat activity with thresholds, spikes and clips marked (for tuning)."""

from __future__ import annotations

from pathlib import Path

from . import ClipperError
from .detect import Moment, Window


def save_plot(wins: list[Window], moments: list[Moment], dest: Path, title: str = "") -> Path:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise ClipperError("--plot needs matplotlib: pip install matplotlib") from e

    x = [w.start / 3600 for w in wins]
    fig, ax = plt.subplots(figsize=(16, 5), dpi=110)
    ax.plot(x, [w.score for w in wins], color="#4a6fa5", lw=0.8, label="window score")
    ax.plot(x, [w.baseline for w in wins], color="#8a8a8a", lw=1, label="baseline")
    ax.plot(x, [w.threshold for w in wins], color="#d08c3b", lw=1, ls="--", label="spike threshold")

    sx = [w.start / 3600 for w in wins if w.spike]
    sy = [w.score for w in wins if w.spike]
    ax.scatter(sx, sy, s=10, color="#c0392b", zorder=3, label="spike window")

    mx = [w.start / 3600 for w in wins if w.muted]
    if mx:
        ax.scatter(mx, [0] * len(mx), marker="|", s=40, color="#7d3c98", label="muted (raid/gift)")

    for rank, m in enumerate(moments, 1):
        ax.axvspan(m.clip_start / 3600, m.clip_end / 3600, color="#27ae60", alpha=0.25, lw=0)
        ax.annotate(str(rank), (m.peak_time / 3600, m.peak_score), textcoords="offset points",
                    xytext=(0, 4), ha="center", fontsize=8, color="#1e8449")

    ax.set_xlabel("hours into VOD")
    ax.set_ylabel("score")
    ax.set_title(title or "Chat activity")
    ax.set_xlim(0, max(x) if x else 1)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(dest)
    plt.close(fig)
    return dest
