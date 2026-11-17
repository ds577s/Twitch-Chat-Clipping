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
        with self.assertRaises(server.ApiError):
            server.build_argv("vod", {"inputs": ["123"], "options": {"window": "nan"}})
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


class HttpSecurityTest(unittest.TestCase):
    """The checks that keep other websites and devices out of the dashboard."""

    @classmethod
    def setUpClass(cls):
        import threading
        out = Path(tempfile.mkdtemp())
        (out / "run1").mkdir()
        (out / "run1" / "a.mp4").write_bytes(b"x")
        (out / "secret.txt").write_text("no", encoding="utf-8")
        cls.app = server.App(out, out)
        handler = type("H", (server.Handler,), {"app": cls.app})
        cls.httpd = server.Server(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.app.allowed_hosts = {f"127.0.0.1:{cls.port}"}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, method, path, host=None, token=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if token:
            headers["X-Clipper-Token"] = token
        c.request(method, path, body=b"{}" if method != "GET" else None, headers=headers)
        status = c.getresponse().status
        c.close()
        return status

    def test_wrong_host_refused(self):   # DNS rebinding: another site resolving to 127.0.0.1
        self.assertEqual(self.req("GET", "/api/info", host="evil.example:80"), 403)
        self.assertEqual(self.req("GET", "/api/info"), 200)

    def test_changes_need_the_token(self):
        self.assertEqual(self.req("POST", "/api/jobs"), 403)
        self.assertEqual(self.req("POST", "/api/jobs", token="wrong"), 403)
        self.assertEqual(self.req("POST", "/api/jobs", token=self.app.token), 400)  # past the check: no mode

    def test_media_stays_inside_the_run(self):
        self.assertEqual(self.req("GET", "/media/run1/a.mp4"), 200)
        self.assertEqual(self.req("GET", "/media/run1/..%2Fsecret.txt"), 404)
        self.assertEqual(self.req("GET", "/media/..%2Fsecret.txt"), 400)   # ".." is not a run name


class DeleteClipTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.run = self.out / "123"
        (self.run / "clean").mkdir(parents=True)
        for f in ("01_a.mp4", "01_a.srt", "clean/01_a.mp4", "clean/01_a.words.json", "02_b.mp4"):
            (self.run / f).write_bytes(b"x")
        clips = [{"rank": 1, "file": "01_a.mp4", "layout": "center", "captions": {"words": 3}, "top_messages": []},
                 {"rank": 2, "file": "02_b.mp4", "top_messages": []}]
        (self.run / "report.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")
        self.app = server.App(self.out, self.out)

    def test_deletes_files_and_marks_report(self):
        self.app.delete_clip({"session": "123", "rank": 1})
        left = sorted(str(p.relative_to(self.run)).replace("\\", "/") for p in self.run.rglob("*") if p.is_file())
        self.assertEqual(left, ["02_b.mp4", "report.csv", "report.json"])
        clips = json.loads((self.run / "report.json").read_text(encoding="utf-8"))["clips"]
        self.assertEqual(clips[0], {"rank": 1, "file": None, "deleted": True, "top_messages": []})
        self.assertEqual(clips[1]["file"], "02_b.mp4")
        with self.assertRaises(server.ApiError):   # nothing left to delete
            self.app.delete_clip({"session": "123", "rank": 1})

    def test_refused_while_run_is_busy(self):
        class Busy:
            running, sessions = True, ["123"]
        self.app.jobs["x"] = Busy()
        with self.assertRaises(server.ApiError) as e:
            self.app.delete_clip({"session": "123", "rank": 1})
        self.assertEqual(e.exception.status, 409)
        self.assertTrue((self.run / "01_a.mp4").exists())
