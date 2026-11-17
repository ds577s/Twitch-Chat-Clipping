"""Live mode: watch a channel's chat in real time, keep a rolling video buffer, and cut a
clip about a minute after each chat spike. Optionally run VOD mode when the stream ends
(--vod-after) to catch the best moments ranked across the whole stream.

Detection differs from VOD mode in one way: the future isn't known yet. So a window is
first judged against the trailing ~5 minutes. When a spike is over (chat has calmed down
for --merge-gap seconds), its peak is re-checked against a baseline centred on it using
the chat that came after. That rejects sustained rises (more viewers, a new game), which
look like spikes to a trailing baseline but aren't moments.
"""

from __future__ import annotations

import argparse
import bisect
import json
import queue
import shutil
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import ClipperError, captions, config, events, facecam, report, tools, video
from .chat import CleanStats, Message, VodMeta, classify, normalize
from .cli import detect_params, load_scoring_filters, process_vod
from .config import FilterConfig, ScoringConfig
from .detect import (DetectParams, Moment, Window, baseline_threshold, moment_from_group,
                     set_clip_bounds, summarize_chat, window_score)
from .livechat import LiveChat
from .recorder import Recorder, streamlink_cmd

WARMUP_SECONDS = 60      # no spikes until there's a minute of chat to compare against
CHAT_LAG = 1.5           # wait this long after a window ends before scoring it
LIVE_LATENCY = 4.0       # our video arrives a few seconds after broadcast; used to match VOD times


# --------------------------------------------------------------------------- detection
class LiveDetector:
    """Feed it chat as it arrives; tick() returns finished moments."""

    def __init__(self, scoring: ScoringConfig, filters: FilterConfig, p: DetectParams, t0: float):
        self.scoring, self.filters, self.p, self.t0 = scoring, filters, p, t0
        self.windows: list[Window] = []                 # scored windows, in order
        self._acc: dict[int, list] = {}                 # idx -> [per_user Counter, counted, kw, messages]
        self.msgs: list[Message] = []                   # recent kept messages, time relative to t0
        self.msgs_total = 0                             # kept messages ever (msgs drops old ones)
        self.stats = CleanStats()
        self._raid_hits: Counter = Counter()
        self._group: list[Window] = []
        self._last_spike_end = 0.0
        self._cap = scoring.per_user_cap if scoring.per_user_cap > 0 else float("inf")
        self._k = max(1, round(p.baseline_span / p.window))  # trailing windows in the baseline

    def add_message(self, wall: float, user: str, body: str, bits: int = 0) -> None:
        t = wall - self.t0
        if t < 0:
            return
        self.stats.total += 1
        body = normalize(body)
        reason = classify(user, body, bits, self.filters)
        if reason == "raid message":
            w = int(t // self.p.window)
            self._raid_hits[w] += 1
            if self._raid_hits[w] == self.filters.raid_min_messages:
                s = w * self.p.window
                self._mute(s, s + self.p.window + self.filters.raid_mute_seconds, "raid (raid messages)")
        if reason:
            self.stats.dropped[reason] += 1
            return
        self.stats.kept += 1
        self.msgs.append(Message(t=t, user=user or "?", body=body))
        self.msgs_total += 1
        idx = max(int(t // self.p.window), len(self.windows))  # very late message -> next open window
        acc = self._acc.setdefault(idx, [Counter(), 0, 0.0, 0])
        acc[0][user] += 1
        acc[3] += 1
        if acc[0][user] <= self._cap:
            acc[1] += 1
            acc[2] += self.scoring.keyword_weight(body)[0]

    def add_gift(self, wall: float, count: int) -> None:
        if count >= self.filters.gift_min_count:
            t = wall - self.t0
            self._mute(t, t + self.filters.gift_mute_seconds, f"gift bomb ({count} subs)")

    def add_raid(self, wall: float, viewers: int) -> None:
        t = wall - self.t0
        self._mute(t, t + self.filters.raid_mute_seconds, f"raid ({viewers} viewers)")

    def _mute(self, start: float, end: float, reason: str) -> None:
        self.stats.mutes.append((max(0.0, start), end, reason))

    def _muted(self, start: float) -> str | None:
        for s, e, r in self.stats.mutes:
            if start < e and start + self.p.window > s:
                return r
        return None

    def tick(self, wall_now: float) -> list[tuple[Moment, str | None]]:
        """Score every window that has finished. Returns (moment, reject_reason) for each
        spike that ended; reject_reason is None for accepted moments."""
        p, out = self.p, []
        t_now = wall_now - self.t0
        while (len(self.windows) + 1) * p.window + CHAT_LAG <= t_now:
            i = len(self.windows)
            per_user, counted, kw, n = self._acc.pop(i, None) or [Counter(), 0, 0.0, 0]
            w = Window(start=i * p.window, messages=n, counted=counted, unique=len(per_user), keyword_bonus=kw)
            w.score = window_score(counted, kw, w.unique, self.scoring)
            w.muted = self._muted(w.start)
            w.baseline, w.threshold, w.z = baseline_threshold(
                w.score, [x.score for x in self.windows[-self._k:]], p)
            w.spike = w.start >= WARMUP_SECONDS and w.muted is None and w.score >= w.threshold
            self.windows.append(w)
            if w.spike:
                self._group.append(w)
                self._last_spike_end = w.start + p.window
            elif self._group and w.start + p.window - self._last_spike_end > p.merge_gap:
                out.append(self._close(t_now))
        if len(self.msgs) > 50_000:
            self._trim_msgs(t_now)
        return out

    def _trim_msgs(self, t_now: float) -> None:
        """Forget chat no spike can still need (only the spike's own span is summarized),
        so memory stays flat on long, busy streams. chat_live.jsonl keeps everything."""
        keep_from = t_now - (self.p.max_len + self.p.merge_gap + 2 * self.p.window)
        if self._group:
            keep_from = min(keep_from, self._group[0].start)
        cut = bisect.bisect_left(self.msgs, keep_from, key=lambda m: m.t)
        del self.msgs[:cut]

    def flush(self, wall_now: float) -> list[tuple[Moment, str | None]]:
        """Stream ended: close any spike still in progress."""
        out = self.tick(wall_now)
        if self._group:
            out.append(self._close(wall_now - self.t0))
        return out

    def _close(self, t_now: float) -> tuple[Moment, str | None]:
        group, self._group = self._group, []
        m = moment_from_group(group, self.p)
        i = int(m.peak_time // self.p.window)
        h = max(1, self._k // 2)
        neigh = [x.score for x in self.windows[max(0, i - h):i] + self.windows[i + 1:i + 1 + h]]
        base, thr, _ = baseline_threshold(m.peak_score, neigh, self.p)
        set_clip_bounds(m, t_now, self.p)
        summarize_chat(m, self.msgs, self.scoring)
        if m.peak_score < thr:
            return m, f"sustained rise in chat, not a spike (peak {m.peak_score:.0f} vs centred threshold {thr:.0f})"
        m.baseline, m.ratio = base, m.peak_score / max(base, 1.0)
        return m, None


# --------------------------------------------------------------------------- clip worker
class ClipWorker(threading.Thread):
    """Cuts and renders clips in the background so chat keeps being read."""

    def __init__(self, rec: Recorder, sess_dir: Path, ffmpeg: str, layout: str,
                 candidates: list[dict], on_done, capcfg=None, streamer: str | None = None,
                 streamers_path: Path | None = None):
        super().__init__(daemon=True, name="clip-worker")
        self.rec, self.sess_dir, self.ffmpeg = rec, sess_dir, ffmpeg
        self.layout, self.candidates = layout, candidates
        self.streamer, self.streamers_path = streamer, streamers_path
        self.capcfg = capcfg
        self.can_detect = facecam.available()
        self.on_done = on_done
        self.jobs: queue.Queue = queue.Queue()
        self.pending: dict[int, float] = {}
        self._plock = threading.Lock()

    def submit(self, rec_entry: dict, wall_a: float, wall_b: float) -> None:
        with self._plock:
            self.pending[rec_entry["rank"]] = wall_a
        self.jobs.put((rec_entry, wall_a, wall_b))

    def oldest_needed(self) -> float | None:
        with self._plock:
            return min(self.pending.values()) if self.pending else None

    def _release(self, rec_entry: dict) -> None:
        with self._plock:
            self.pending.pop(rec_entry["rank"], None)

    def finish(self) -> None:
        self.jobs.put(None)

    def run(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            rec_entry, a, b = job
            try:
                self._make(rec_entry, a, b)
            except Exception as e:   # any error: this clip fails, the worker keeps going
                msg = str(e) if isinstance(e, ClipperError) else f"{type(e).__name__}: {e}"
                rec_entry["file"] = None
                rec_entry["error"] = msg.splitlines()[0] if msg else type(e).__name__
                print(f"  ! clip {rec_entry['rank']} failed: {msg}")
            finally:
                self._release(rec_entry)
                try:
                    self.on_done()
                except Exception as e:
                    print(f"  ! couldn't save the report: {type(e).__name__}: {e}")
                events.emit("clip", rank=rec_entry["rank"], status="failed" if rec_entry.get("error") else "done")

    def current_candidates(self) -> list[dict]:
        """Facecam boxes for this clip. With the stacked layout streamers.yaml is read again for
        every clip, so positions saved in Setup during a live run apply from the next clip."""
        if self.layout != "stacked":
            return []
        if self.streamer:
            try:
                boxes = config.load_facecams(self.streamer, self.streamers_path)
            except Exception as e:   # e.g. the file is mid-edit: keep the last good positions
                print(f"  ! couldn't re-read the facecam positions ({e}); using the previous ones")
            else:
                if boxes != self.candidates:
                    print(f"  facecam positions for '{self.streamer}' updated: {len(boxes)} position(s)"
                          + ("" if boxes else ", so clips use the center crop"))
                    self.candidates = boxes
        return self.candidates

    def _make(self, rec_entry: dict, a: float, b: float) -> None:
        deadline = time.time() + 60
        while (self.rec.covered_until() or 0) < b and self.rec.running() and time.time() < deadline:
            time.sleep(1)  # wait until the end of the clip has been recorded
        raw = self.sess_dir / "raw" / f"{rec_entry['rank']:02d}.mp4"
        raw.parent.mkdir(parents=True, exist_ok=True)
        self.rec.cut(a, b, raw)
        self._release(rec_entry)   # cut: the buffer may drop this part now, even while rendering/captioning
        cands = self.current_candidates()
        cam = cands[0] if cands else None
        if cands and self.can_detect:
            idx, _hits = facecam.pick(raw, cands)
            cam = cands[idx] if idx is not None else None
            rec_entry["facecam"] = idx + 1 if idx is not None else None
        rec_entry["layout"] = "stacked" if cam else "center"
        video.render_vertical(raw, self.sess_dir / rec_entry["file"], self.ffmpeg, rec_entry["layout"], cam)
        raw.unlink(missing_ok=True)
        if self.capcfg:
            add_captions(self.sess_dir, rec_entry, self.capcfg, self.ffmpeg)
        print(f"  -> clip {rec_entry['rank']} ready: {rec_entry['file']}")


def add_captions(run_dir: Path, rec_entry: dict, cfg, ffmpeg: str) -> None:
    """Caption a freshly rendered clip. A captions failure keeps the clip, without captions."""
    try:
        info = captions.caption_clip(run_dir, rec_entry["file"], rec_entry.get("layout", "center"), cfg,
                                     ffmpeg, fresh=True)
        rec_entry["captions"] = info
        rec_entry.pop("captions_error", None)
        print(f"  captions: {info['words']} words" + (f" ({info['language']})" if info.get("language") else ""))
    except ClipperError as e:
        rec_entry.pop("captions", None)
        rec_entry["captions_error"] = str(e).splitlines()[0]
        print(f"  ! captions failed, clip kept without them: {e}")


# --------------------------------------------------------------------------- session
def channel_name(s: str) -> str:
    s = s.strip().rstrip("/")
    if "twitch.tv/" in s:
        s = s.split("twitch.tv/", 1)[1].split("/")[0].split("?")[0]
    if not s or not all(c.isalnum() or c == "_" for c in s):
        raise ClipperError(f"Not a Twitch channel name or URL: {s!r}")
    return s.lower()


def _clock(wall: float) -> str:
    return datetime.fromtimestamp(wall).strftime("%Hh%Mm%Ss")


def write_chat_json(log_path: Path, dest: Path, channel: str, t0: float, length: float) -> None:
    """Convert the live chat log into the same JSON shape TwitchDownloaderCLI produces, so it
    can be replayed with:  clipper live_session --chat-file chat.json --dry-run --plot"""
    comments = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = e["t"] - t0
            if t < 0:
                continue
            if e["type"] == "msg":
                body, bits, user = e["body"], e.get("bits", 0), e["user"]
            elif e["type"] == "gift":
                user, bits = e["user"], 0
                body = f"{user} is gifting {e['count']} Tier 1 Subs to {channel}'s community!"
            elif e["type"] == "raid":
                user, bits = e["user"], 0
                body = f"{user} is raiding with a party of {e['viewers']}."
            else:
                continue
            comments.append({"content_offset_seconds": round(t, 3),
                             "commenter": {"name": user, "display_name": user},
                             "message": {"body": body, "bits_spent": bits}})
    data = {"streamer": {"name": channel, "login": channel},
            "video": {"title": f"live session {datetime.fromtimestamp(t0):%Y-%m-%d %H:%M}",
                      "length": round(length), "created_at": datetime.fromtimestamp(t0).astimezone().isoformat()},
            "comments": comments}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def run(args: argparse.Namespace) -> int:
    channel = channel_name(args.channel)
    found = tools.require(["ffmpeg", "streamlink"])
    for t in tools.TOOLS:
        found.setdefault(t, tools.find(t))
    if args.vod_after and not (found.get("yt-dlp") and found.get("twitchdownloader")):
        print("note: --vod-after needs yt-dlp (to find the VOD) and TwitchDownloaderCLI; it will be skipped.")
    capcfg = captions.from_args(args)
    if capcfg:   # load the speech model while waiting for the stream, not at the first clip
        threading.Thread(target=captions.preload, args=(capcfg,), daemon=True, name="caption-model").start()

    scoring, filters = load_scoring_filters(args)
    p = detect_params(args, max_clips=args.max_clips or 10**9)

    streamer = channel if args.streamer == "auto" else args.streamer
    candidates = config.load_facecams(streamer, args.streamers) if args.layout == "stacked" else []
    if args.layout == "stacked" and not candidates:
        print(f"no facecam position for '{streamer}' in streamers.yaml yet; clips use the center crop "
              "until you save one (Setup → Facecam), which applies from the next clip")

    sess_dir = Path(args.out) / f"live_{channel}_{datetime.now():%Y%m%d_%H%M%S}"
    sess_dir.mkdir(parents=True, exist_ok=True)
    events.emit("session", mode="live", dir=str(sess_dir.resolve()), name=sess_dir.name, channel=channel)
    events.emit("state", state="waiting")
    rec = Recorder(sess_dir / "buffer", found["ffmpeg"],
                   source=streamlink_cmd(found["streamlink"], channel, args.quality, args.oauth, wait=True))
    chat_q: queue.Queue = queue.Queue()
    chat = LiveChat(channel, chat_q)
    chat.start()
    rec.start()
    print(f"Watching twitch.tv/{channel} — waiting for the stream to go live (Ctrl-C to stop)...")

    t0 = None
    stopped_by_user = ended = False
    det = worker = None
    records: list[dict] = []
    accepted: list[Moment] = []
    live_clips: list[tuple[float, float, str]] = []
    chat_log = sess_dir / "chat_live.jsonl"
    log_f = open(chat_log, "a", encoding="utf-8")
    report_lock = threading.Lock()
    hour_counts: Counter = Counter()

    def save_report():
        with report_lock:
            if det is None:
                return
            meta = VodMeta(vod_id=sess_dir.name, streamer=channel, title="live session",
                           length=time.time() - t0)
            try:
                report.write_reports(sess_dir, meta, p, det.stats, records)
                report.write_activity(sess_dir, p.window, list(det.windows))
            except OSError as e:   # e.g. Windows keeps the file locked: the next save tries again
                print(f"  ! couldn't save the report this time: {e}")
                return
        events.emit("report")

    try:
        # Wait for the stream to start. Chat from before that is ignored.
        while t0 is None:
            _drain_info(chat_q)
            if events.stop_requested():
                raise KeyboardInterrupt
            t0 = rec.first_segment_time()
            if t0 is None and not rec.running():
                raise ClipperError(f"streamlink stopped before the stream started:\n{rec.log_tail()}")
            time.sleep(0.5)
        print(f"[{_clock(time.time())}] stream is live — recording (buffer {args.buffer:.0f}s) and watching chat")
        events.emit("state", state="live", t0=t0)
        det = LiveDetector(scoring, filters, p, t0)
        worker = ClipWorker(rec, sess_dir, found["ffmpeg"], args.layout, candidates, save_report, capcfg,
                            streamer=streamer, streamers_path=args.streamers)
        worker.start()
        save_report()  # so the session (and the dashboard's view of it) exists from the start

        last_cleanup = last_status = time.time()
        reconnecting_since: float | None = None
        seen_until = rec.covered_until()
        sent_windows = sent_msgs = 0

        while True:
            try:
                ev = chat_q.get(timeout=0.5)
            except queue.Empty:
                ev = None
            while ev is not None:
                _handle_event(ev, det, log_f)
                try:
                    ev = chat_q.get_nowait()
                except queue.Empty:
                    ev = None

            if events.stop_requested():
                raise KeyboardInterrupt
            now = time.time()
            for m, why in det.tick(now):
                _consider(m, why, det, worker, records, accepted, live_clips, hour_counts, args, t0)
            if events.enabled() and len(det.windows) > sent_windows:
                unsent = det.msgs_total - sent_msgs
                new_msgs = det.msgs[-unsent:] if unsent else []
                sent_msgs = det.msgs_total
                for w in det.windows[sent_windows:]:
                    events.emit("window", row=report.window_row(w))
                sent_windows = len(det.windows)
                events.emit("chat", messages=[[m.user, m.body[:200]] for m in new_msgs[-8:]])

            if now - last_cleanup > 5:
                last_cleanup = now
                keep = now - args.buffer
                need = worker.oldest_needed()
                rec.cleanup(min(keep, need) if need else keep)
                log_f.flush()
            if now - last_status > 60:
                last_status = now
                recent = [w for w in det.windows[-6:]]
                rate = sum(w.messages for w in recent)
                print(f"[{_clock(now)}] live {report.hms(now - t0, False)} | {rate} msgs/min | "
                      f"{len(records)} clips")
                rec_ok = rec.covered_until()
                events.emit("status", elapsed=now - t0, rate=rate, clips=len(records),
                            video_lag=round(now - rec_ok, 1) if rec_ok else None)
                save_report()

            if args.stop_after and now - t0 >= args.stop_after * 60:
                print(f"[{_clock(now)}] --stop-after {args.stop_after:g} min reached.")
                stopped_by_user = True
                break

            # Recorder health: a stream that stops may come back (network blip) or be over.
            cov = rec.covered_until()
            if cov != seen_until:
                seen_until, reconnecting_since = cov, None
            if not rec.running():
                if reconnecting_since is None:
                    print(f"[{_clock(now)}] stream video stopped — checking if it comes back (up to ~2 min)...")
                    events.emit("state", state="reconnecting")
                    reconnecting_since = now
                    rec.stop()
                    rec.source = streamlink_cmd(found["streamlink"], channel, args.quality, args.oauth, wait=False)
                    rec.start()
                else:
                    print(f"[{_clock(now)}] stream has ended.")
                    ended = True
                    break
    except KeyboardInterrupt:
        stopped_by_user = True
        print("\nstopping...")
    except ClipperError as e:
        print(f"error: {e}")
        rec.stop()
        chat.stop()
        log_f.close()
        return 1
    except Exception:
        rec.stop()   # never leave streamlink/ffmpeg recording after we're gone
        chat.stop()
        log_f.close()
        raise

    # Shutdown: stop inputs, close any spike in progress, finish pending clips.
    events.emit("state", state="finishing", ended=ended)
    rec.stop()
    chat.stop()
    log_f.close()
    if det is not None:
        for m, why in det.flush(time.time()):
            _consider(m, why, det, worker, records, accepted, live_clips, hour_counts, args, t0)
        if worker is not None:
            n = len(worker.pending)
            worker.finish()
            if n:
                print(f"finishing {n} clip(s) in progress (Ctrl-C again to abandon them)...")
            try:
                while worker.is_alive():
                    worker.join(0.5)
            except KeyboardInterrupt:
                print("abandoned unfinished clips")
        save_report()
        length = (det.windows[-1].start + p.window) if det.windows else 0.0
        write_chat_json(chat_log, sess_dir / "chat.json", channel, t0, length)
        if args.plot and det.windows:
            from .plot import save_plot
            save_plot(det.windows, accepted, sess_dir / "chat_activity.png", title=f"{channel} live")
        save_report()
        meta = VodMeta(vod_id=sess_dir.name, streamer=channel, length=length)
        report.print_summary(meta, det.stats, records)
    if not args.keep_raw:
        shutil.rmtree(sess_dir / "buffer", ignore_errors=True)
        shutil.rmtree(sess_dir / "raw", ignore_errors=True)
    print(f"\nLive session saved to {sess_dir}")
    events.emit("report")
    events.emit("state", state="done", ended=ended)

    if args.vod_after and det is not None:
        if stopped_by_user and not ended:
            print("Skipping --vod-after because you stopped before the stream ended. Run VOD mode later:\n"
                  "  clipper https://www.twitch.tv/videos/<id>")
        elif found.get("yt-dlp") and found.get("twitchdownloader"):
            return _vod_after(channel, args, live_clips, t0, found)
    return 0


def _drain_info(q: queue.Queue) -> None:
    while True:
        try:
            ev = q.get_nowait()
        except queue.Empty:
            return
        if ev[0] == "info":
            print(f"[{_clock(ev[1])}] {ev[2]}")


def _handle_event(ev, det: LiveDetector, log_f) -> None:
    kind, wall = ev[0], ev[1]
    if kind == "msg":
        _, _, user, body, bits = ev
        det.add_message(wall, user, body, bits)
        log_f.write(json.dumps({"t": round(wall, 3), "type": "msg", "user": user, "body": body,
                                "bits": bits}, ensure_ascii=False) + "\n")
    elif kind == "gift":
        det.add_gift(wall, ev[3])
        log_f.write(json.dumps({"t": round(wall, 3), "type": "gift", "user": ev[2], "count": ev[3]}) + "\n")
    elif kind == "raid":
        det.add_raid(wall, ev[3])
        log_f.write(json.dumps({"t": round(wall, 3), "type": "raid", "user": ev[2], "viewers": ev[3]}) + "\n")
        print(f"[{_clock(wall)}] raid from {ev[2]} ({ev[3]} viewers) — muting chat for "
              f"{det.filters.raid_mute_seconds:.0f}s")
    elif kind == "info":
        print(f"[{_clock(wall)}] {ev[2]}")


def _consider(m: Moment, why: str | None, det: LiveDetector, worker: ClipWorker, records: list,
              accepted: list, live_clips: list, hour_counts: Counter, args, t0: float) -> None:
    when = _clock(t0 + m.peak_time)
    top = ", ".join(t[:20] for t, _ in m.top_messages[:3])
    hour = int(m.peak_time // 3600)
    if not why and args.max_clips and len(records) >= args.max_clips:
        why = f"--max-clips {args.max_clips} reached"
    elif not why and args.max_per_hour and hour_counts[hour] >= args.max_per_hour:
        why = f"--max-per-hour {args.max_per_hour} reached"
    if why:
        print(f"[{when}] spike (score {m.peak_score:.0f}) skipped: {why}  ({top})")
        events.emit("skipped", peak_time=m.peak_time, clock=when, score=round(m.peak_score, 1), reason=why,
                    top=[t for t, _ in m.top_messages[:3]])
        return
    hour_counts[hour] += 1
    n = len(records) + 1
    a = t0 + m.clip_start + args.video_delay
    b = t0 + m.clip_end + args.video_delay
    fname = f"{n:02d}_{_clock(a)}_score{int(round(m.peak_score))}.mp4"
    rec = report.moment_record(n, m, None, fname)
    rec["clock_time"] = datetime.fromtimestamp(a).strftime("%Y-%m-%d %H:%M:%S")
    accepted.append(m)
    records.append(rec)
    live_clips.append((a, b, fname))
    print(f"[{when}] SPIKE score {m.peak_score:.0f} ({m.ratio:.1f}x baseline) — cutting clip {n} "
          f"({b - a:.0f}s): {top}")
    worker.submit(rec, a, b)
    events.emit("clip", rank=n, status="working")


def latest_vod_id(ytdlp: str, channel: str) -> str | None:
    proc = tools.run([ytdlp, "--flat-playlist", "--playlist-end", "1", "--print", "id",
                      f"https://www.twitch.tv/{channel}/videos?filter=archives&sort=time"],
                     "Finding the stream's VOD")
    ids = [x.strip().lstrip("v") for x in proc.stdout.splitlines() if x.strip()]
    return ids[0] if ids and ids[0].isdigit() else None


def _vod_after(channel: str, args, live_clips, t0: float, found: dict) -> int:
    print("\n=== VOD follow-up: ranking the whole stream ===")
    events.emit("state", state="vod")
    try:
        time.sleep(30)  # give Twitch a moment to finalize the VOD
        vod_id = latest_vod_id(found["yt-dlp"], channel)
        if not vod_id:
            raise ClipperError(f"No VOD found for {channel}. Are past broadcasts disabled on this channel?")
        vargs = argparse.Namespace(**vars(args))
        vargs.chat_file, vargs.refresh_chat, vargs.dry_run = None, False, False
        vargs.downloader = "auto"
        vargs.max_clips = args.vod_max_clips
        vargs.quality = None if args.quality in ("best", "worst") else args.quality
        scoring, filters = load_scoring_filters(args)
        process_vod(vod_id, vargs, scoring, filters, found, live_clips=live_clips, must_cover=t0)
    except ClipperError as e:
        print(f"error: {e}")
        return 1
    return 0
