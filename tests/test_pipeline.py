"""Synthetic-chat tests: run with  python -m unittest discover tests"""

import json
import random
import tempfile
import unittest
from pathlib import Path

from clipper import chat as chatmod
from clipper import config
from clipper.chat import parse_vod_id
from clipper.detect import DetectParams, detect


def _c(t, user, body, bits=0):
    return {"content_offset_seconds": t, "commenter": {"name": user, "display_name": user},
            "message": {"body": body, "bits_spent": bits}}


def make_chat(seed=1):
    """3-hour VOD. Hour 1 quiet (~3 msg/10s), hours 2-3 busy (~25 msg/10s).
    Real moments at 0:30:00 (quiet hour) and 2:00:00 and 2:40:00 (busy hours).
    Fake events: bot spam 0:45:00, single-user spam 1:10:00, gift bomb 1:30:00, raid 2:20:00."""
    rnd = random.Random(seed)
    filler = ["yo", "gg", "nice", "what is this game", "hello chat", "lets go", "true", "LUL nah", "ok"]
    comments = []
    length = 3 * 3600

    for t10 in range(0, length, 10):
        rate = 3 if t10 < 3600 else 25
        for _ in range(rnd.randint(int(rate * 0.6), int(rate * 1.4))):
            comments.append(_c(t10 + rnd.random() * 10, f"viewer{rnd.randint(1, 3000)}", rnd.choice(filler)))

    def burst(at, n, bodies, span=20):
        for k in range(n):
            comments.append(_c(at + rnd.random() * span, f"reactor{at}_{k}", rnd.choice(bodies)))

    burst(1800, 40, ["KEKW", "LMAO", "💀💀💀", "CLIP IT", "????"])            # quiet-hour moment
    burst(7200, 200, ["NO WAY", "WTF", "????", "CLIP", "KEKW KEKW"])        # big busy-hour moment
    burst(9600, 160, ["OMEGALUL", "LMAO", "ICANT", "clip that"])            # second busy-hour moment

    for k in range(80):  # bot spam
        comments.append(_c(2700 + k * 0.2, "nightbot", "Follow the socials! !discord"))
    for k in range(100):  # one person spamming
        comments.append(_c(4200 + k * 0.1, "spammer", "KEKW CLIP IT KEKW"))
    comments.append(_c(5400, "bigspender", "bigspender is gifting 50 Tier 1 Subs to Streamer's community!"))
    for k in range(50):
        comments.append(_c(5400 + k * 0.05, "bigspender", f"bigspender gifted a Tier 1 sub to user{k}!"))
    burst(5402, 60, ["W", "Pog", "THANK YOU", "W W W"])                     # chat reacting to gifts
    burst(8400, 150, ["otherstreamerRaid otherstreamerRaid", "RAID HYPE raid"], span=15)
    burst(8402, 120, ["Pog", "hi from the raid", "LUL"])                    # raiders saying hi
    burst(3300, 60, ["1", "1", "2", "1 " + chr(0x034F)])                                # streamer-prompted poll
    burst(length - 60, 150, ["WW STREAM", "gn", "W"])                       # outro goodbyes
    for k in range(30):
        comments.append(_c(3000 + k, f"cheerer{k}", "Cheer100 great stream", bits=100))
        comments.append(_c(3100 + k, f"cmd{k}", "!enter"))

    comments.sort(key=lambda c: c["content_offset_seconds"])
    return {"streamer": {"name": "teststreamer"}, "video": {"title": "test", "length": length},
            "comments": comments}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.scoring = config.load_scoring()
        self.filters = config.load_filters()
        self.tmp = tempfile.TemporaryDirectory()
        self.chat_path = Path(self.tmp.name) / "chat.json"
        self.chat_path.write_text(json.dumps(make_chat()), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_detect(self, **kw):
        meta, comments = chatmod.load_chat(self.chat_path, "1")
        p = DetectParams(**kw)
        msgs, stats = chatmod.clean(comments, self.filters, p.window, meta.length)
        wins, moments = detect(msgs, meta.length, self.scoring, p, stats.mutes)
        return meta, stats, moments

    def assert_found(self, moments, t, label):
        hits = [m for m in moments if m.spike_start - 20 <= t <= m.spike_end + 5]
        self.assertTrue(hits, f"{label} at {t}s not found; got {[(m.spike_start, m.peak_score) for m in moments]}")
        m = hits[0]
        self.assertLessEqual(m.clip_start, t, f"{label}: clip should start before the moment")

    def assert_absent(self, moments, start, end, label):
        bad = [m for m in moments if start <= m.peak_time <= end]
        self.assertFalse(bad, f"{label} produced a clip: {[(m.peak_time, m.peak_score) for m in bad]}")

    def test_real_moments_found_fakes_rejected(self):
        for method in ("ratio", "zscore"):
            with self.subTest(method=method):
                meta, stats, moments = self.run_detect(method=method)
                self.assertEqual(meta.length, 3 * 3600)
                self.assert_found(moments, 1800, "quiet-hour moment")
                self.assert_found(moments, 7200, "busy moment 1")
                self.assert_found(moments, 9600, "busy moment 2")
                self.assert_absent(moments, 2690, 2730, "bot spam")
                self.assert_absent(moments, 4190, 4220, "single-user spam")
                self.assert_absent(moments, 5390, 5460, "gift bomb")
                self.assert_absent(moments, 8390, 8500, "raid")
                self.assert_absent(moments, 2990, 3140, "cheers/commands")
                self.assert_absent(moments, 3290, 3330, "poll votes")
                self.assert_absent(moments, 3 * 3600 - 150, 3 * 3600, "stream outro")

    def test_cleaning_stats(self):
        _, stats, _ = self.run_detect()
        self.assertEqual(stats.dropped["bot"], 80)
        self.assertEqual(stats.dropped["command"], 30)
        self.assertEqual(stats.dropped["bits"], 30)
        self.assertGreaterEqual(stats.dropped["system notice"], 51)
        reasons = " ".join(r for _, _, r in stats.mutes)
        self.assertIn("raid", reasons)
        self.assertIn("gift bomb", reasons)

    def test_clip_bounds(self):
        _, _, moments = self.run_detect(pre_roll=25, post_roll=10, min_len=15, max_len=60)
        for m in moments:
            d = m.clip_end - m.clip_start
            self.assertGreaterEqual(d, 15 - 1e-6)
            self.assertLessEqual(d, 60 + 1e-6)
            self.assertGreaterEqual(m.clip_start, 0)
            self.assertLessEqual(m.clip_end, 3 * 3600)
        self.assertTrue(moments[0].top_messages)

    def test_ranking_and_max_clips(self):
        _, _, moments = self.run_detect(max_clips=2)
        self.assertEqual(len(moments), 2)
        self.assertGreaterEqual(moments[0].peak_score, moments[1].peak_score)
        self.assertAlmostEqual(moments[0].spike_start, 7200, delta=30)

    def test_parse_vod_id(self):
        self.assertEqual(parse_vod_id("https://www.twitch.tv/videos/123456789"), "123456789")
        self.assertEqual(parse_vod_id("https://twitch.tv/videos/123456789?t=1h2m3s"), "123456789")
        self.assertEqual(parse_vod_id("123456789"), "123456789")
        with self.assertRaises(Exception):
            parse_vod_id("https://www.twitch.tv/somechannel")

    def test_keywords(self):
        kw = self.scoring.keyword_weight
        self.assertEqual(kw("W")[1], "hype")
        self.assertEqual(kw("WOW")[1], None)
        self.assertEqual(kw("CLIP IT KEKW")[1], "clip")
        self.assertEqual(kw("💀")[1], "funny")
        self.assertEqual(kw("what???")[1], "hype")
        self.assertEqual(kw("WWWW")[1], "hype")
        self.assertEqual(kw("wwwww stream")[1], "hype")
        self.assertEqual(kw("wow")[1], None)

    def test_raid_pattern(self):
        p = self.filters.raid_pattern
        for s in ["RAID HYPE", "raiders here", "otherstreamerRaid", "hi from the raid"]:
            self.assertTrue(p.search(s), s)
        for s in ["im afraid", "braided hair", "AFRAID"]:
            self.assertFalse(p.search(s), s)

    def test_normalize_invisible_chars(self):
        self.assertEqual(chatmod.normalize("? " + chr(0x034F)), "?")
        self.assertEqual(chatmod.normalize("KEKW" + chr(0x200B) + "  KEKW " + chr(0xE0000)), "KEKW KEKW")


if __name__ == "__main__":
    unittest.main()
