"""Live-mode tests: python -m unittest discover tests"""

import random
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from clipper import chat as chatmod
from clipper import config, tools
from clipper.detect import DetectParams
from clipper.live import LiveDetector, channel_name, write_chat_json
from clipper.livechat import event_from_line, parse_line
from clipper.recorder import Recorder, streamlink_cmd

T0 = 1_700_000_000.0  # arbitrary wall-clock start for replayed streams


def make_stream(seed=7, sudden_jump=False):
    """Synthetic 3h stream as live events [(t, kind, ...)], in time order.
    Quiet first hour (~3 msgs/10s), busy afterwards (~25/10s): a 5-minute ramp, or an instant
    jump with sudden_jump=True. Real moments at 0:30:00, 2:00:00, 2:40:00."""
    rnd = random.Random(seed)
    filler = ["yo", "gg", "nice", "what is this game", "hello chat", "lets go", "true", "LUL nah", "ok"]
    ev = []
    for t10 in range(0, 3 * 3600, 10):
        if sudden_jump:
            rate = 3 if t10 < 3600 else 25
        else:
            rate = 3 + 22 * min(1.0, max(0.0, (t10 - 3600) / 300))
        for _ in range(rnd.randint(int(rate * 0.6), int(rate * 1.4))):
            ev.append((t10 + rnd.random() * 10, "msg", f"viewer{rnd.randint(1, 3000)}", rnd.choice(filler), 0))

    def burst(at, n, bodies, span=20):
        for k in range(n):
            ev.append((at + rnd.random() * span, "msg", f"reactor{at}_{k}", rnd.choice(bodies), 0))

    burst(1800, 40, ["KEKW", "LMAO", "💀💀💀", "CLIP IT", "????"])
    burst(7200, 200, ["NO WAY", "WTF", "????", "CLIP", "KEKW KEKW"])
    burst(9600, 160, ["OMEGALUL", "LMAO", "ICANT", "clip that"])
    for k in range(80):
        ev.append((2700 + k * 0.2, "msg", "nightbot", "Follow the socials! !discord", 0))
    for k in range(100):
        ev.append((4200 + k * 0.1, "msg", "spammer", "KEKW CLIP IT KEKW", 0))
    ev.append((5400, "gift", "bigspender", 50))                       # tagged USERNOTICE live
    burst(5402, 60, ["W", "Pog", "THANK YOU", "W W W"])
    burst(8400, 150, ["otherstreamerRaid otherstreamerRaid", "RAID HYPE raid"], span=15)
    burst(8402, 120, ["Pog", "hi from the raid", "LUL"])
    burst(3300, 60, ["1", "1", "2"])
    for k in range(30):
        ev.append((3000 + k, "msg", f"cheerer{k}", "Cheer100 great stream", 100))
        ev.append((3100 + k, "msg", f"cmd{k}", "!enter", 0))
    ev.sort(key=lambda e: e[0])
    return ev


def replay(events, p=None):
    det = LiveDetector(config.load_scoring(), config.load_filters(), p or DetectParams(), T0)
    accepted, rejected = [], []
    last_tick = -1
    for e in events:
        t = e[0]
        if int(t) != last_tick:  # tick once per simulated second, like the real loop
            last_tick = int(t)
            for m, why in det.tick(T0 + t):
                (rejected if why else accepted).append(m)
        if e[1] == "msg":
            det.add_message(T0 + t, e[2], e[3], e[4])
        elif e[1] == "gift":
            det.add_gift(T0 + t, e[3])
    for m, why in det.flush(T0 + events[-1][0] + 120):
        (rejected if why else accepted).append(m)
    return det, accepted, rejected


class LiveDetectorTest(unittest.TestCase):
    def test_finds_real_moments_and_ignores_fakes(self):
        for method in ("ratio", "zscore"):
            with self.subTest(method=method):
                det, acc, _ = replay(make_stream(), DetectParams(method=method))
                peaks = [m.peak_time for m in acc]
                for t, label in ((1800, "quiet-hour moment"), (7200, "busy 1"), (9600, "busy 2")):
                    self.assertTrue(any(t - 10 <= x <= t + 40 for x in peaks), f"{label} missed: {peaks}")
                fakes = [(2690, 2730, "bot"), (4190, 4220, "spammer"), (5390, 5460, "gift bomb"),
                         (8390, 8500, "raid"), (3290, 3330, "poll"), (2990, 3140, "cheers")]
                if method == "ratio":
                    # z-score with a trailing baseline is noisier live and can fire during the ramp;
                    # ratio (the default) is checked to reject it, and exactly 3 clips over several seeds.
                    fakes.append((3600, 3900, "ramp up"))
                for lo, hi, label in fakes:
                    self.assertFalse([x for x in peaks if lo <= x <= hi], f"{label} produced a clip: {peaks}")
                for m in acc:
                    self.assertLessEqual(m.clip_start, m.peak_time)
                    self.assertTrue(m.top_messages)

    def test_ratio_exactly_real_moments_across_seeds(self):
        for seed in range(1, 6):
            _, acc, _ = replay(make_stream(seed=seed))
            peaks = sorted(m.peak_time for m in acc)
            self.assertEqual(len(peaks), 3, f"seed {seed}: {peaks}")
            for got, want in zip(peaks, (1800, 7200, 9600)):
                self.assertTrue(want - 10 <= got <= want + 40, f"seed {seed}: {peaks}")

    def test_sudden_lasting_jump_is_not_a_clip(self):
        _, acc, rej = replay(make_stream(sudden_jump=True))
        self.assertFalse([m for m in acc if 3590 <= m.peak_time <= 3900],
                         f"jump clipped: {[m.peak_time for m in acc]}")
        self.assertTrue(any(3590 <= m.peak_time <= 3900 for m in rej), "jump should show up as rejected")

    def test_clip_ready_soon_after_moment(self):
        """A spike must be emitted within ~a minute of chat calming down, not at stream end."""
        det = LiveDetector(config.load_scoring(), config.load_filters(), DetectParams(), T0)
        rnd = random.Random(1)
        got = None
        for s in range(0, 400):
            n = 40 if 200 <= s < 215 else rnd.randint(0, 1)
            for k in range(n):
                det.add_message(T0 + s + k / max(n, 1), f"u{s}_{k}", "KEKW" if n > 2 else "hi")
            for m, why in det.tick(T0 + s + 1):
                if not why and got is None:
                    got = s
        self.assertIsNotNone(got, "spike never emitted")
        self.assertLess(got - 215, 60, f"emitted {got - 215}s after the spike ended")

    def test_chat_log_round_trip(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            log = tmp / "chat_live.jsonl"
            log.write_text('{"t": 1700000010.5, "type": "msg", "user": "a", "body": "KEKW", "bits": 0}\n'
                           '{"t": 1700000020.0, "type": "gift", "user": "b", "count": 20}\n'
                           '{"t": 1699999999.0, "type": "msg", "user": "early", "body": "x", "bits": 0}\n',
                           encoding="utf-8")
            write_chat_json(log, tmp / "chat.json", "chan", T0, 60)
            meta, comments = chatmod.load_chat(tmp / "chat.json", "live")
            self.assertEqual(meta.streamer, "chan")
            self.assertEqual(len(comments), 2)
            self.assertAlmostEqual(comments[0]["content_offset_seconds"], 10.5)
            _, stats = chatmod.clean(comments, config.load_filters(), 10)
            self.assertTrue(any("gift bomb" in r for _, _, r in stats.mutes))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LiveChatParseTest(unittest.TestCase):
    def test_privmsg(self):
        line = ("@badge-info=;bits=100;display-name=Some\\sOne;tmi-sent-ts=1 "
                ":someone!someone@someone.tmi.twitch.tv PRIVMSG #chan :Cheer100 hello there")
        self.assertEqual(event_from_line(line, 5.0), ("msg", 5.0, "someone", "Cheer100 hello there", 100))
        tags, *_ = parse_line(line)
        self.assertEqual(tags["display-name"], "Some One")

    def test_action_and_colon_in_body(self):
        line = ":x!x@x.tmi.twitch.tv PRIVMSG #chan :\x01ACTION waves: hi\x01"
        self.assertEqual(event_from_line(line, 1.0)[3], "waves: hi")

    def test_usernotices(self):
        gift = "@msg-id=submysterygift;login=rich;msg-param-mass-gift-count=50 :tmi.twitch.tv USERNOTICE #chan"
        raid = "@msg-id=raid;login=other;msg-param-viewerCount=1234 :tmi.twitch.tv USERNOTICE #chan"
        sub = "@msg-id=resub;login=fan :tmi.twitch.tv USERNOTICE #chan :love the stream"
        self.assertEqual(event_from_line(gift, 1.0), ("gift", 1.0, "rich", 50))
        self.assertEqual(event_from_line(raid, 1.0), ("raid", 1.0, "other", 1234))
        self.assertIsNone(event_from_line(sub, 1.0))

    def test_channel_name(self):
        self.assertEqual(channel_name("https://www.twitch.tv/IShowSpeed"), "ishowspeed")
        self.assertEqual(channel_name("ishowspeed"), "ishowspeed")
        with self.assertRaises(Exception):
            channel_name("not a channel!")

    def test_streamlink_cmd(self):
        cmd = streamlink_cmd("streamlink", "chan", "720p60", wait=False)
        self.assertIn("720p60,best", cmd)
        self.assertEqual(cmd[cmd.index("--retry-max") + 1], "4")


@unittest.skipUnless(tools.find("ffmpeg"), "ffmpeg not installed")
class RecorderTest(unittest.TestCase):
    def test_record_cut_cleanup(self):
        tmp = Path(tempfile.mkdtemp())
        ff = tools.find("ffmpeg")
        rec = Recorder(tmp / "buf", ff,
                       input_args=["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                                   "-f", "lavfi", "-i", "sine=frequency=440"],
                       encode_args=["-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                                    "-c:a", "aac", "-shortest"])
        try:
            rec.start()
            deadline = time.time() + 30
            while len(rec.segments()) < 4 and time.time() < deadline:
                time.sleep(0.5)
            self.assertTrue(rec.running(), rec.log_tail())
            segs = rec.segments()
            self.assertGreaterEqual(len(segs), 4, rec.log_tail())
            t_first = segs[0][0]
            out = tmp / "clip.mp4"
            rec.cut(t_first + 2, t_first + 8, out)
            import subprocess
            dur = float(subprocess.run([ff.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
                                        "format=duration", "-of", "csv=p=0", str(out)],
                                       capture_output=True, text=True).stdout.strip())
            self.assertGreater(dur, 5)
            self.assertLess(dur, 10)
            removed = rec.cleanup(keep_after=segs[-1][0])
            self.assertGreaterEqual(removed, 1)
            self.assertGreaterEqual(len(rec.segments()), 1)
        finally:
            rec.stop()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
