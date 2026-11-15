"""Dashboard server: building commands, saving facecams, events."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from clipper import events
from clipper.config import DEFAULT_DIR, load_facecams
from clipper.ui import server


class BuildArgvTest(unittest.TestCase):
    def test_vod_only_changed_options(self):
        argv, title = server.build_argv("vod", {"inputs": ["https://www.twitch.tv/videos/123456"],
                                                "options": {"multiplier": 2.5, "max_clips": 15, "dry_run": True,
                                                            "layout": "stacked", "only": "1,3"}})
        self.assertEqual(argv, ["123456", "--multiplier", "2.5", "--layout", "stacked", "--dry-run", "--only", "1,3"])
        self.assertIn("preview", title)

    def test_live(self):
        argv, _ = server.build_argv("live", {"channel": "twitch.tv/SomeOne", "options": {"vod_after": True, "stop_after": 120}})
        self.assertEqual(argv, ["live", "someone", "--vod-after", "--stop-after", "120"])

    def test_rejects_unsafe(self):
        with self.assertRaises(server.ApiError):
            server.build_argv("vod", {"inputs": ["123"], "options": {"layout": "sideways"}})
        with self.assertRaises(server.ApiError):
            server.build_argv("vod", {"inputs": ["123"], "options": {"streamer": "a;rm -rf /"}})
        with self.assertRaises(server.ApiError):
            server.build_argv("vod", {"inputs": ["123"], "options": {"multiplier": -1}})
        # paths and secrets are never taken from the page
        argv, _ = server.build_argv("vod", {"inputs": ["123"], "options": {"keywords": "C:/x.yaml", "oauth": "abc",
                                                                            "out": "C:/", "chat_file": "x"}})
        self.assertEqual(argv, ["123"])

    def test_parse_time(self):
        self.assertEqual(server.parse_time("1:02:03"), 3723)
        self.assertEqual(server.parse_time("1h2m3s"), 3723)
        self.assertEqual(server.parse_time("90"), 90)
        self.assertEqual(server.parse_time("5:00"), 300)


class SaveFacecamsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "streamers.yaml"
        self.original = (DEFAULT_DIR / "streamers.yaml").read_bytes()
        self.tmp.write_bytes(self.original)

    def test_add_replace_remove_round_trip(self):
        server.save_facecams(self.tmp, "newguy", [{"x": 10, "y": 20, "w": 300, "h": 200}], 720)
        boxes = load_facecams("newguy", self.tmp)
        self.assertEqual(boxes, [{"x": 10, "y": 20, "w": 300, "h": 200, "ref_height": 720}])
        self.assertEqual(len(load_facecams("ishowspeed", self.tmp)), 2)   # others untouched

        server.save_facecams(self.tmp, "newguy", [{"x": 1, "y": 2, "w": 30, "h": 40},
                                                  {"x": 5, "y": 6, "w": 70, "h": 80}], 1080)
        self.assertEqual(len(load_facecams("newguy", self.tmp)), 2)

        server.save_facecams(self.tmp, "newguy", [], 1080)
        self.assertEqual(self.tmp.read_bytes(), self.original)   # byte-for-byte, comments and newlines kept

    def test_replace_existing_keeps_rest(self):
        server.save_facecams(self.tmp, "examplestreamer", [{"x": 1, "y": 1, "w": 50, "h": 50}], 1080)
        self.assertEqual(load_facecams("examplestreamer", self.tmp)[0]["w"], 50)
        self.assertEqual(len(load_facecams("ishowspeed", self.tmp)), 2)
        self.assertIn(b"# Facecam positions", self.tmp.read_bytes())


class EventsTest(unittest.TestCase):
    def test_emit_and_stop(self):
        d = Path(tempfile.mkdtemp())
        old = {k: os.environ.get(k) for k in ("CLIPPER_EVENTS", "CLIPPER_STOP_FILE")}
        try:
            os.environ.pop("CLIPPER_EVENTS", None)
            events.emit("nothing")                     # no-op without the env var
            os.environ["CLIPPER_EVENTS"] = str(d / "ev.jsonl")
            os.environ["CLIPPER_STOP_FILE"] = str(d / "stop")
            events.emit("clip", rank=3, status="done")
            ev = json.loads((d / "ev.jsonl").read_text(encoding="utf-8"))
            self.assertEqual((ev["type"], ev["rank"]), ("clip", 3))
            self.assertFalse(events.stop_requested())
            (d / "stop").touch()
            self.assertTrue(events.stop_requested())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
