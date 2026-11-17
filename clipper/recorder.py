"""Record a live stream into a rolling buffer of short segments, and cut clips from it.

streamlink -> stdout -> ffmpeg segment muxer. Segments are named after the UTC wall-clock
time they started (seg_YYYYmmdd-HHMMSS.ts), which is the same clock the chat reader uses,
so a chat spike at wall time W maps straight onto the video received at W. UTC, not local
time, so names never repeat when the clocks go back.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ClipperError
from . import tools

SEGMENT_SECONDS = 4
_NAME_FMT = "%Y%m%d-%H%M%S"


class Recorder:
    def __init__(self, seg_dir: Path, ffmpeg: str, source: list[str] | None = None,
                 input_args: list[str] | None = None, encode_args: list[str] | None = None,
                 log_path: Path | None = None):
        """`source`: command whose stdout is the stream (streamlink ... --stdout).
        `input_args`: ffmpeg input args (default: read the source from stdin).
        `encode_args`: default stream copy; tests pass an encoder for synthetic input."""
        self.seg_dir = seg_dir
        self.ffmpeg = ffmpeg
        self.source = source
        self.input_args = input_args or ["-i", "pipe:0"]
        self.encode_args = encode_args or ["-c", "copy"]
        self.log_path = log_path or seg_dir / "recorder.log"
        self._src: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._log = None
        self.lock = threading.Lock()   # held while cutting, so cleanup doesn't delete in-use files

    # --- process control -------------------------------------------------
    def start(self) -> None:
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "ab")
        if self.source:
            self._src = subprocess.Popen(self.source, stdout=subprocess.PIPE, stderr=self._log,
                                         stdin=subprocess.DEVNULL)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               *([] if self.source else ["-nostdin"]),
               *self.input_args,
               "-map", "0:v:0", "-map", "0:a:0?", *self.encode_args,
               "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mpegts",
               "-strftime", "1", str(self.seg_dir / f"seg_{_NAME_FMT}.ts")]
        self._ff = subprocess.Popen(cmd, stdin=self._src.stdout if self._src else subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=self._log,
                                    env={**os.environ, "TZ": "UTC0"})  # -strftime names in UTC
        if self._src:
            self._src.stdout.close()  # ffmpeg owns the pipe now

    def running(self) -> bool:
        procs = [p for p in (self._src, self._ff) if p]
        return bool(procs) and all(p.poll() is None for p in procs)

    def stop(self, timeout: float = 10) -> None:
        for p in (self._src, self._ff):
            if p and p.poll() is None:
                p.terminate()
        for p in (self._src, self._ff):
            if p:
                try:
                    p.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self._log:
            self._log.close()
            self._log = None

    def log_tail(self, n: int = 10) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    # --- segments ----------------------------------------------------------
    def segments(self) -> list[tuple[float, Path]]:
        """(start wall time, path) for every segment on disk, oldest first."""
        out = []
        for p in self.seg_dir.glob("seg_*.ts"):
            try:
                out.append((datetime.strptime(p.stem[4:], _NAME_FMT).replace(tzinfo=timezone.utc).timestamp(), p))
            except ValueError:
                continue
        return sorted(out)

    def first_segment_time(self) -> float | None:
        segs = self.segments()
        return segs[0][0] if segs else None

    def covered_until(self) -> float | None:
        """Wall time up to which video is fully written (start of the newest segment)."""
        segs = self.segments()
        return segs[-1][0] if segs else None

    def cut(self, start: float, end: float, dest: Path) -> None:
        """Write wall-clock range [start, end] to `dest` (.mp4) without re-encoding.
        Starts on the keyframe at or before `start`, so it may begin a second or two early."""
        with self.lock:
            segs = self.segments()
            chosen = [(s, p) for i, (s, p) in enumerate(segs)
                      if s <= end and (i + 1 == len(segs) or segs[i + 1][0] > start)]
            if not chosen:
                raise ClipperError("that part of the stream isn't in the buffer (recording gap?)")
            joined = dest.with_suffix(".ts")
            with open(joined, "wb") as out:      # MPEG-TS segments can be joined byte-for-byte
                for _, p in chosen:
                    out.write(p.read_bytes())
        offset = max(0.0, start - chosen[0][0])
        try:
            tools.run([self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                       "-ss", f"{offset:.2f}", "-i", str(joined), "-t", f"{end - start:.2f}",
                       "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                       "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(dest)],
                      "Cutting clip from buffer")
        finally:
            joined.unlink(missing_ok=True)

    def cleanup(self, keep_after: float) -> int:
        """Delete segments that end before wall time `keep_after`. Returns how many."""
        removed = 0
        with self.lock:
            segs = self.segments()
            for i, (s, p) in enumerate(segs[:-1]):  # never the newest (still being written)
                if segs[i + 1][0] < keep_after:
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed


def streamlink_cmd(exe: str, channel: str, quality: str = "best", oauth: str | None = None,
                   wait: bool = True) -> list[str]:
    """streamlink command writing the stream to stdout. `wait`: keep polling until the channel
    goes live; otherwise give up after ~2 minutes (used to tell a blip from the stream ending)."""
    q = quality if quality in ("best", "worst") else f"{quality},best"
    # Streamlink 6+ skips Twitch ad segments by itself (the old --twitch-disable-ads flag is gone).
    cmd = [exe, "--stdout", "--loglevel", "warning", "--twitch-low-latency",
           "--retry-streams", "30", "--retry-max", "0" if wait else "4",
           f"twitch.tv/{channel}", q]
    if oauth:
        cmd[1:1] = ["--twitch-api-header", f"Authorization=OAuth {oauth}"]
    return cmd


def wait_for_first_segment(rec: Recorder, poll: float = 1.0, stop: threading.Event | None = None) -> float | None:
    """Block until the first segment appears (stream is live). None if the recorder died or stop was set."""
    while not (stop and stop.is_set()):
        t = rec.first_segment_time()
        if t is not None:
            return t
        if not rec.running():
            return None
        time.sleep(poll)
    return None
