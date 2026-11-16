"""Captions: grouping words, the .ass/.srt files, settings, burning with ffmpeg."""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from clipper import ClipperError, captions, tools
from clipper.captions import CaptionConfig, Word
from clipper.ui import server


def words(*items):
    """words(("hello", 0.0, 0.4), ...)"""
    return [Word(a, b, t) for t, a, b in items]


SAMPLE = words(("No", 0.00, 0.20), ("way", 0.25, 0.50), ("he", 0.55, 0.70), ("hit", 0.75, 0.95),
               ("that.", 1.00, 1.30), ("Chat", 2.50, 2.80), ("is", 2.85, 3.00), ("crazy!", 3.05, 3.60))


class ChunkTest(unittest.TestCase):
    def test_groups_by_count_pause_and_sentence_end(self):
        chunks = captions.chunk_words(SAMPLE, per=3)
        self.assertEqual([[w.text for w in c] for c in chunks],
                         [["No", "way", "he"], ["hit", "that."], ["Chat", "is", "crazy!"]])

    def test_long_words_split_early(self):
        long = words(("Supercalifragilistic", 0, 0.5), ("expialidocious", 0.5, 1.0), ("yes", 1.0, 1.2))
        self.assertEqual(len(captions.chunk_words(long, per=3, max_chars=22)), 2)


class TextTest(unittest.TestCase):
    def test_replace_and_mask(self):
        cfg = CaptionConfig(replace={"speed": "Speed"}, mask_swears=True).validate()
        out = captions.clean_words(words(("speed,", 0, 1), ("fucking", 1, 2), ("ok", 2, 3)), cfg)
        self.assertEqual([w.text for w in out], ["Speed,", "f******", "ok"])

    def test_no_mask_by_default(self):
        out = captions.clean_words(words(("shit", 0, 1)), CaptionConfig().validate())
        self.assertEqual(out[0].text, "shit")


class AssTest(unittest.TestCase):
    def test_ass_highlights_each_word_and_positions(self):
        cfg = CaptionConfig(highlight="#4ADE80", color="#FFFFFF").validate()
        chunks = captions.chunk_words(SAMPLE, per=3)
        ass = captions.build_ass(chunks, cfg, "stacked")
        dialogues = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
        self.assertEqual(len(dialogues), len(SAMPLE))                  # one event per spoken word
        self.assertIn("&H0080DE4A", ass)                                # #4ADE80 as &H00BBGGRR
        self.assertTrue(all("\\pos(540,640)" in d for d in dialogues))  # facecam / gameplay seam
        self.assertIn("{\\c&H0080DE4A&\\fscx108\\fscy108}NO{\\r} WAY HE", dialogues[0])
        self.assertTrue(dialogues[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.25,"))
        # captions never overlap: each chunk ends by the time the next one starts
        times = [tuple(d.split(",")[1:3]) for d in dialogues]
        self.assertEqual(times[2][1], "0:00:00.75")
        self.assertIn("\\pos(540,1300)", captions.build_ass(chunks, cfg, "center"))
        cfg.position = "bottom"
        self.assertIn("\\pos(540,1480)", captions.build_ass(chunks, cfg, "stacked"))

    def test_ass_escapes_override_braces(self):
        cfg = CaptionConfig(uppercase=False).validate()
        ass = captions.build_ass([words(("{\\b1}hi", 0, 1))], cfg, "center")
        self.assertIn("(b1)hi", ass)
        self.assertNotIn("{\\b1}", ass)

    def test_srt(self):
        srt = captions.build_srt(captions.chunk_words(SAMPLE, per=3), CaptionConfig(uppercase=False).validate())
        self.assertTrue(srt.startswith("1\n00:00:00,000 --> 00:00:00,750\nNo way he\n"))
        self.assertIn("3\n00:00:02,500 --> 00:00:03,850\nChat is crazy!", srt)


class ConfigTest(unittest.TestCase):
    def test_save_load_round_trip(self):
        p = Path(tempfile.mkdtemp()) / "captions.yaml"
        cfg = CaptionConfig(model="base", language="no", words_per_caption=2, font="Impact",
                            vocabulary=["iShowSpeed", "KEKW: lol"], replace={"ice speed": "iShowSpeed", "a: b": "c"})
        captions.save_config(cfg, p)
        back = captions.load_config(p)
        self.assertEqual(back, cfg.validate())
        self.assertEqual(back.language, "no")          # not read as YAML's boolean "no"
        captions.save_config(back, p)
        self.assertTrue(p.with_suffix(".yaml.bak").exists())

    def test_default_file_loads(self):
        self.assertIsInstance(captions.load_config(), CaptionConfig)

    def test_rejects_bad_values(self):
        for bad in ({"model": "huge"}, {"color": "white"}, {"position": "left"}, {"font": "x;y{}"},
                    {"language": "english"}):
            with self.assertRaises(ClipperError):
                CaptionConfig(**bad).validate()
        self.assertEqual(CaptionConfig(size=999).validate().size, 200)   # clamped


class BuildArgvTest(unittest.TestCase):
    def test_captions_option_reaches_the_command(self):
        argv, _ = server.build_argv("vod", {"inputs": ["123"], "options": {"captions": True, "caption_model": "base"}})
        self.assertEqual(argv, ["123", "--captions", "--caption-model", "base"])
        argv, _ = server.build_argv("live", {"channel": "x", "options": {"captions": True}})
        self.assertEqual(argv, ["live", "x", "--captions"])
        with self.assertRaises(server.ApiError):
            server.build_argv("vod", {"inputs": ["123"], "options": {"caption_model": "huge"}})
        argv, _ = server.build_argv("vod", {"inputs": ["123"], "options": {"captions_file": "C:/x.yaml"}})
        self.assertEqual(argv, ["123"])


@unittest.skipUnless(tools.find("ffmpeg"), "needs ffmpeg")
class BurnTest(unittest.TestCase):
    """Caption a tiny generated clip from a saved transcript (no speech model needed)."""

    def test_caption_clip_from_saved_transcript(self):
        ffmpeg = tools.find("ffmpeg")
        run = Path(tempfile.mkdtemp()) / "run with spaces"
        (run / "clean").mkdir(parents=True)
        clip = "01_00h00m00s_score10.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:d=2:r=30",
                        "-f", "lavfi", "-i", "sine=d=2", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", str(run / clip)], check=True)
        cfg = CaptionConfig(model="tiny").validate()
        # A transcript saved by an earlier run, so no transcription is needed.
        (run / "clean" / clip).write_bytes((run / clip).read_bytes())
        (run / "clean" / "01_00h00m00s_score10.words.json").write_text(json.dumps(
            {"model": "tiny", "language_setting": "auto", "vocabulary": [], "language": "en",
             "words": [[0.1, 0.5, "Hello"], [0.6, 1.2, "chat!"]]}), encoding="utf-8")
        before = (run / clip).stat().st_size
        info = captions.caption_clip(run, clip, "stacked", cfg, ffmpeg, fresh=False)
        self.assertEqual(info, {"words": 2, "language": "en", "model": "tiny", "srt": "01_00h00m00s_score10.srt"})
        self.assertTrue((run / clip).exists() and (run / clip).stat().st_size != before)
        self.assertTrue((run / "clean" / clip).exists())                  # clean copy kept
        self.assertFalse(list((run / "clean").glob("*.ass")))             # temp file removed
        self.assertFalse(list(run.glob("*.partial.mp4")))
        self.assertIn("HELLO CHAT!", (run / "01_00h00m00s_score10.srt").read_text(encoding="utf-8"))
        probe = subprocess.run([ffmpeg, "-i", str(run / clip)], capture_output=True, text=True).stderr
        self.assertTrue(re.search(r"1080x1920", probe))
        self.assertIn("Audio", probe)

        captions.forget(run, clip)                                        # re-cut without captions
        self.assertFalse((run / "clean" / clip).exists())
        self.assertFalse((run / "01_00h00m00s_score10.srt").exists())


if __name__ == "__main__":
    unittest.main()
