"""Speech captions: transcribe a clip with faster-whisper (on this PC, free) and burn
TikTok-style captions into it, with the word being spoken highlighted.

Each captioned clip keeps an uncaptioned copy and its transcript in `clean/`, so captions can
be restyled later without downloading or transcribing again:

  <run>/01_00h21m45s_score278.mp4        captioned clip
  <run>/01_00h21m45s_score278.srt        plain subtitles (for TikTok's own caption tool etc.)
  <run>/clean/01_00h21m45s_score278.mp4  the same clip without captions
  <run>/clean/01_00h21m45s_score278.words.json   word timings from Whisper
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from . import ClipperError, tools
from .config import DEFAULT_DIR

MODELS = ["tiny", "base", "small", "medium", "large-v3", "turbo"]
POSITIONS = ["auto", "top", "middle", "bottom"]
SAMPLE_RATE = 16000
INSTALL = 'pip install "faster-whisper>=1.1"   (or: pip install -e ".[captions]")'

# Lines Whisper tends to invent over music or silence.
_HALLUCINATIONS = re.compile(
    r"^(thanks?( you)? (so much )?for watching|please subscribe|subscribe to (my|the) channel|"
    r"subtitles? by|transcribed by|thank you\.?)[.!]*$", re.IGNORECASE)


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


# --------------------------------------------------------------------------- settings
@dataclass
class CaptionConfig:
    model: str = "small"
    language: str = "auto"
    device: str = "auto"              # auto (GPU if it works, else CPU), cpu, cuda
    words_per_caption: int = 3
    font: str = "Arial Black"
    size: int = 84                    # pixels on the 1080x1920 frame
    uppercase: bool = True
    color: str = "#FFFFFF"
    highlight: str = "#4ADE80"
    outline: int = 6
    position: str = "auto"            # auto: between facecam and gameplay, or lower middle
    mask_swears: bool = False
    vocabulary: list[str] = field(default_factory=list)
    replace: dict[str, str] = field(default_factory=dict)
    swears: list[str] = field(default_factory=lambda: list(_DEFAULT_SWEARS))

    def validate(self) -> "CaptionConfig":
        if self.model not in MODELS:
            raise ClipperError(f"captions: model must be one of {', '.join(MODELS)}")
        if self.device not in ("auto", "cpu", "cuda"):
            raise ClipperError("captions: device must be auto, cpu or cuda")
        if self.position not in POSITIONS:
            raise ClipperError(f"captions: position must be one of {', '.join(POSITIONS)}")
        if not re.fullmatch(r"auto|[a-z]{2,3}", str(self.language)):
            raise ClipperError("captions: language must be auto or a code like en, es, de")
        for name in ("color", "highlight"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(getattr(self, name))):
                raise ClipperError(f"captions: {name} must look like #RRGGBB")
        if not re.fullmatch(r"[\w .\-]{1,48}", self.font):
            raise ClipperError("captions: font name has unexpected characters")
        self.words_per_caption = _clamp(self.words_per_caption, 1, 8, "words_per_caption")
        self.size = _clamp(self.size, 30, 200, "size")
        self.outline = _clamp(self.outline, 0, 20, "outline")
        self.uppercase, self.mask_swears = bool(self.uppercase), bool(self.mask_swears)
        self.vocabulary = [str(w).strip() for w in (self.vocabulary or []) if str(w).strip()][:200]
        self.replace = {str(k).strip(): str(v).strip() for k, v in (self.replace or {}).items() if str(k).strip()}
        self.swears = [str(w).strip().lower() for w in (self.swears or []) if str(w).strip()]
        return self


def _clamp(v, lo: int, hi: int, name: str) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ClipperError(f"captions: {name} must be a whole number") from None
    return max(lo, min(hi, v))


_DEFAULT_SWEARS = ["fuck", "shit", "bitch", "cunt", "dick", "pussy", "asshole", "bastard", "motherfuck"]


def load_config(path: Path | None = None) -> CaptionConfig:
    p = Path(path) if path else DEFAULT_DIR / "captions.yaml"
    data: dict = {}
    if p.exists():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ClipperError(f"Could not parse {p}:\n{e}") from e
    known = set(CaptionConfig.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ClipperError(f"{p}: unknown setting(s): {', '.join(sorted(unknown))}")
    return CaptionConfig(**data).validate()


def save_config(cfg: CaptionConfig, path: Path | None = None) -> Path:
    """Write captions.yaml (validated first), keeping a .bak of the previous file."""
    cfg.validate()
    p = Path(path) if path else DEFAULT_DIR / "captions.yaml"
    d = asdict(cfg)
    q = lambda s: json.dumps(s, ensure_ascii=False)   # JSON strings are valid YAML scalars
    lines = [
        "# Captions burned into clips (--captions, or the Captions switch in the dashboard).",
        "# Edit here or on the dashboard's Captions page.",
        "",
        f"model: {d['model']}            # {', '.join(MODELS)}: bigger is more accurate but slower",
        f"language: {q(d['language'])}        # auto, or a code like en, es, de",
        f"device: {d['device']}            # auto uses the GPU if it works, else the CPU",
        "",
        f"words_per_caption: {d['words_per_caption']}",
        f"font: {q(d['font'])}",
        f"size: {d['size']}                # pixels on the 1080x1920 clip",
        f"uppercase: {str(d['uppercase']).lower()}",
        f"color: {q(d['color'])}",
        f"highlight: {q(d['highlight'])}      # the word being spoken",
        f"outline: {d['outline']}",
        f"position: {d['position']}          # auto (between facecam and gameplay, or lower middle), top, middle, bottom",
        f"mask_swears: {str(d['mask_swears']).lower()}",
        "",
        "# Names and slang Whisper should spell right (streamers, games, emotes).",
        "vocabulary:" + (" []" if not d["vocabulary"] else ""),
        *[f"  - {q(w)}" for w in d["vocabulary"]],
        "",
        "# Words Whisper gets wrong, and what to write instead.",
        "replace:" + (" {}" if not d["replace"] else ""),
        *[f"  {q(k)}: {q(v)}" for k, v in d["replace"].items()],
        "",
        "# Masked (f***) when mask_swears is on. Words starting with these count too.",
        "swears:" + (" []" if not d["swears"] else ""),
        *[f"  - {q(w)}" for w in d["swears"]],
        "",
    ]
    text = "\n".join(lines)
    load_check = yaml.safe_load(text)                     # never write something we can't read back
    CaptionConfig(**load_check).validate()
    if p.exists():
        shutil.copyfile(p, p.with_suffix(".yaml.bak"))
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return p


def from_args(args) -> CaptionConfig | None:
    """The caption settings for a run, or None when --captions is off."""
    if not getattr(args, "captions", False):
        return None
    if not available():
        raise ClipperError("--captions needs faster-whisper:  " + INSTALL)
    cfg = load_config(getattr(args, "captions_file", None))
    if getattr(args, "caption_model", None):
        cfg.model = args.caption_model
    if getattr(args, "caption_language", None):
        cfg.language = args.caption_language
    return cfg.validate()


# --------------------------------------------------------------------------- transcription
@dataclass
class Word:
    start: float
    end: float
    text: str


_model_lock = threading.Lock()
_model_cache: dict = {}
_gpu_failed = False


def _get_model(cfg: CaptionConfig, device: str):
    from faster_whisper import WhisperModel
    key = (cfg.model, device)
    with _model_lock:
        if key not in _model_cache:
            compute = "float16" if device == "cuda" else "int8"
            _model_cache[key] = WhisperModel(cfg.model, device=device, compute_type=compute)
        return _model_cache[key]


def _cuda_visible() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


_gpu_status: str | None = None


def gpu_status() -> str:
    """'ready', 'no-libs' (NVIDIA GPU but CUDA libraries missing) or 'none'."""
    global _gpu_status
    if _gpu_status is None:
        if not available() or not _cuda_visible():
            _gpu_status = "none"
        else:
            import ctypes
            name = "cublas64_12.dll" if sys.platform == "win32" else "libcublas.so.12"
            try:
                ctypes.CDLL(name)
                _gpu_status = "ready"
            except OSError:
                _gpu_status = "no-libs"
    return _gpu_status


def _want_gpu(cfg: CaptionConfig) -> bool:
    # auto: only when the CUDA libraries are actually there; cuda: try it and report errors
    return cfg.device == "cuda" or (cfg.device == "auto" and not _gpu_failed and gpu_status() == "ready")


def preload(cfg: CaptionConfig) -> None:
    """Load (and on first use download) the model now, so the first clip isn't slower.
    Problems are left for the first real transcription to report."""
    if not available():
        return
    try:
        _get_model(cfg, "cuda" if _want_gpu(cfg) else "cpu")
    except Exception as e:
        print(f"  captions: couldn't load the {cfg.model} model yet ({type(e).__name__}: {e})", file=sys.stderr)


def decode_audio(media: Path, ffmpeg: str):
    """16 kHz mono float32 samples via ffmpeg (no PyAV version issues)."""
    import numpy as np
    proc = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-i", str(media), "-vn", "-ac", "1",
                           "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
                          capture_output=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise ClipperError(f"Reading audio from {media.name} failed: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return np.frombuffer(proc.stdout, np.float32).copy()


def transcribe(media: Path, cfg: CaptionConfig, ffmpeg: str) -> tuple[list[Word], str]:
    """Words with timings, and the detected language."""
    global _gpu_failed
    if not available():
        raise ClipperError("Captions need faster-whisper:  " + INSTALL)
    audio = decode_audio(media, ffmpeg)
    if audio.size < SAMPLE_RATE // 2:
        return [], cfg.language if cfg.language != "auto" else ""
    for device in (["cuda", "cpu"] if _want_gpu(cfg) else ["cpu"]):
        try:
            return _run_whisper(_get_model(cfg, device), audio, cfg)
        except Exception as e:   # missing CUDA libraries show up here, on first use
            if device == "cpu" or cfg.device == "cuda":
                if isinstance(e, ClipperError):
                    raise
                raise ClipperError(f"Transcription failed: {type(e).__name__}: {e}") from e
            _gpu_failed = True
            print(f"  captions: GPU not usable ({str(e).splitlines()[0]}); using the CPU. For GPU speed, "
                  "install NVIDIA's cuBLAS and cuDNN for CUDA 12 (see faster-whisper's README).", file=sys.stderr)
    raise AssertionError("unreachable")


def _run_whisper(model, audio, cfg: CaptionConfig) -> tuple[list[Word], str]:
    kw = dict(word_timestamps=True, vad_filter=True, condition_on_previous_text=False, beam_size=5)
    if cfg.language != "auto":
        kw["language"] = cfg.language
    if cfg.vocabulary:
        kw["hotwords"] = ", ".join(cfg.vocabulary)
    segments, info = model.transcribe(audio, **kw)
    words: list[Word] = []
    for seg in segments:          # a generator: decoding happens here
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
            continue
        if _HALLUCINATIONS.match(seg.text.strip()):
            continue
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(round(w.start, 3), round(max(w.end, w.start + 0.05), 3), text))
    return words, info.language


# --------------------------------------------------------------------------- text and layout
def clean_words(words: list[Word], cfg: CaptionConfig) -> list[Word]:
    """Apply the replace list and swear masking (uppercase is applied when drawing)."""
    repl = {k.lower(): v for k, v in cfg.replace.items()}
    out = []
    for w in words:
        m = re.match(r"^(\W*)(.*?)(\W*)$", w.text)
        pre, core, post = m.groups() if m else ("", w.text, "")
        if core.lower() in repl:
            core = repl[core.lower()]
        if cfg.mask_swears and core and any(core.lower().startswith(s) for s in cfg.swears):
            core = core[0] + "*" * (len(core) - 1)
        text = (pre + core + post).strip()
        if text:
            out.append(Word(w.start, w.end, text))
    return out


def chunk_words(words: list[Word], per: int, max_chars: int = 22, gap: float = 0.6) -> list[list[Word]]:
    """Group words into short captions: at most `per` words, broken at pauses and sentence ends."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            ends_sentence = cur[-1].text[-1:] in ".?!,"
            if len(cur) >= per or chars > max_chars or w.start - cur[-1].end > gap or ends_sentence:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def caption_y(position: str, layout: str) -> int:
    from .video import OUT_H, TOP_H
    if position == "top":
        return 330
    if position == "middle":
        return OUT_H // 2
    if position == "bottom":
        return 1480          # above where TikTok / Shorts draw their own buttons and text
    return TOP_H if layout == "stacked" else 1300   # auto: on the facecam / gameplay seam


def _ass_color(hex_rgb: str) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H00{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(s: str, cfg: CaptionConfig) -> str:
    s = s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
    return s.upper() if cfg.uppercase else s


def build_ass(chunks: list[list[Word]], cfg: CaptionConfig, layout: str, length: float | None = None) -> str:
    """An .ass subtitle file: each caption shown while it's spoken, current word highlighted."""
    from .video import OUT_H, OUT_W
    y = caption_y(cfg.position, layout)
    hl = _ass_color(cfg.highlight)
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{cfg.font},{cfg.size},{_ass_color(cfg.color)},{hl},&H00000000,&H96000000,-1,0,0,0,"
        f"100,100,0,0,1,{cfg.outline},{2 if cfg.outline else 0},5,70,70,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for ci, chunk in enumerate(chunks):
        nxt = chunks[ci + 1][0].start if ci + 1 < len(chunks) else None
        end = chunk[-1].end + 0.25
        if nxt is not None:
            end = min(end, nxt)
        if length:
            end = min(end, length)
        for wi, w in enumerate(chunk):
            a = chunk[0].start if wi == 0 else w.start
            b = chunk[wi + 1].start if wi + 1 < len(chunk) else end
            if b - a < 0.02:
                continue
            parts = []
            for k, x in enumerate(chunk):
                t = _ass_text(x.text, cfg)
                parts.append(f"{{\\c{hl}&\\fscx108\\fscy108}}{t}{{\\r}}" if k == wi else t)
            events.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,"
                          f"{{\\an5\\pos({OUT_W // 2},{y})}}" + " ".join(parts))
    return "\n".join(head + events) + "\n"


def build_srt(chunks: list[list[Word]], cfg: CaptionConfig) -> str:
    def ts(t: float) -> str:
        ms = int(round(max(0.0, t) * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    out = []
    for i, c in enumerate(chunks, 1):
        text = " ".join(w.text for w in c)
        end = c[-1].end + 0.25
        if i < len(chunks):
            end = min(end, chunks[i][0].start)
        out.append(f"{i}\n{ts(c[0].start)} --> {ts(end)}\n{text.upper() if cfg.uppercase else text}\n")
    return "\n".join(out)


# --------------------------------------------------------------------------- per clip
def burn(src: Path, dest: Path, ass: Path, ffmpeg: str) -> None:
    """Re-encode `src` with the .ass captions drawn on, into `dest`."""
    tmp = dest.with_name(dest.stem + ".partial.mp4")
    # Run in the .ass folder and pass a bare file name: the ass filter's own escaping rules
    # make full Windows paths (C:\...) painful.
    tools.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src.resolve()),
               "-vf", f"ass={ass.name}", "-map", "0:v", "-map", "0:a?",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", str(tmp.resolve())],
              f"Adding captions to {dest.name}", cwd=ass.parent)
    tmp.replace(dest)


def caption_clip(run_dir: Path, file: str, layout: str, cfg: CaptionConfig, ffmpeg: str,
                 fresh: bool, retranscribe: bool = False) -> dict:
    """Caption `run_dir/file` in place.

    fresh=True: the file was just rendered without captions (it becomes the clean copy).
    fresh=False: re-caption an existing clip from its clean copy (restyle), or caption a clip
    that was cut without captions.
    """
    final = run_dir / file
    clean_dir = run_dir / "clean"
    clean = clean_dir / file
    clean_dir.mkdir(exist_ok=True)
    if fresh or not clean.exists():
        if not final.exists():
            raise ClipperError(f"{file} is missing")
        final.replace(clean)
    stem = Path(file).stem
    cache = clean_dir / f"{stem}.words.json"
    words = lang = None
    if cache.exists() and not retranscribe and not fresh:
        try:
            c = json.loads(cache.read_text(encoding="utf-8"))
            if c.get("model") == cfg.model and c.get("language_setting") == cfg.language \
                    and c.get("vocabulary") == cfg.vocabulary:
                words = [Word(*w) for w in c["words"]]
                lang = c.get("language")
        except (OSError, ValueError, KeyError, TypeError):
            words = None
    try:
        if words is None:
            words, lang = transcribe(clean, cfg, ffmpeg)
            cache.write_text(json.dumps({"model": cfg.model, "language_setting": cfg.language,
                                         "vocabulary": cfg.vocabulary, "language": lang,
                                         "words": [[w.start, w.end, w.text] for w in words]},
                                        ensure_ascii=False), encoding="utf-8")
        chunks = chunk_words(clean_words(words, cfg), cfg.words_per_caption)
        srt = run_dir / f"{stem}.srt"
        if not chunks:          # nobody talking: keep the clip as it is
            shutil.copyfile(clean, final)
            srt.unlink(missing_ok=True)
            return {"words": 0, "language": lang, "model": cfg.model, "srt": None}
        ass = clean_dir / f"{stem}.ass"
        ass.write_text(build_ass(chunks, cfg, layout), encoding="utf-8")
        burn(clean, final, ass, ffmpeg)
        ass.unlink(missing_ok=True)
        srt.write_text(build_srt(chunks, cfg), encoding="utf-8")
        return {"words": len(words), "language": lang, "model": cfg.model, "srt": srt.name}
    except Exception:
        if not final.exists():          # never leave the run without its clip
            shutil.copyfile(clean, final)
        raise


def forget(run_dir: Path, file: str) -> None:
    """A clip was re-cut without captions: drop its stale clean copy and subtitles."""
    stem = Path(file).stem
    for p in (run_dir / "clean" / file, run_dir / "clean" / f"{stem}.words.json", run_dir / f"{stem}.srt"):
        p.unlink(missing_ok=True)
