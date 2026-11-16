"""`clipper ui`: a dashboard in your browser, served from this PC only.

Runs are started as normal `python -m clipper ...` subprocesses, so the dashboard does exactly
what the command line does. Each run writes progress events to a file (see events.py) that the
page polls. Nothing listens on anything but 127.0.0.1, and every request that changes something
must carry a random token that only the page served by this process knows.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import ClipperError, __version__, captions, config, facecam, tools
from ..chat import parse_vod_id

STATIC = Path(__file__).parent / "static"
CONFIG_FILES = ("keywords.yaml", "filters.yaml", "streamers.yaml")

# Options the page may not set: file paths, secrets, output location (fixed by the server).
_BLOCKED_DESTS = {"help", "version", "out", "oauth", "keywords", "filters", "streamers", "chat_file",
                  "inputs", "channel", "captions_file"}
_SAFE_STR = re.compile(r"^[\w\-.:, ]{1,64}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- jobs
class Job:
    def __init__(self, mode: str, argv: list[str], title: str, work: Path):
        self.id = uuid.uuid4().hex[:8]
        self.mode, self.argv, self.title = mode, argv, title
        self.dir = work / self.id
        self.dir.mkdir(parents=True)
        self.events_path = self.dir / "events.jsonl"
        self.stop_path = self.dir / "stop"
        self.log: list[str] = []
        self.events: list[dict] = []
        self.sessions: list[str] = []     # output folder names, in order
        self.state = "starting"
        self.stage = ""
        self.started = time.time()
        self.ended: float | None = None
        self.returncode: int | None = None
        self.stop_requested = False
        self.proc: subprocess.Popen | None = None
        self._ev_offset = 0
        self._lock = threading.Lock()

    def start(self, cwd: Path) -> None:
        env = os.environ.copy()
        env.update(CLIPPER_EVENTS=str(self.events_path), CLIPPER_STOP_FILE=str(self.stop_path),
                   PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "clipper", *self.argv], cwd=cwd, env=env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1)
        threading.Thread(target=self._pump, daemon=True, name=f"job-{self.id}").start()

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            with self._lock:
                self.log.append(line.rstrip("\r\n"))
        self.returncode = self.proc.wait()
        self.ended = time.time()

    @property
    def running(self) -> bool:
        return self.returncode is None

    def stop(self) -> None:
        self.stop_requested = True
        self.stop_path.touch()

    def kill(self) -> None:
        if not (self.proc and self.running):
            return
        if os.name == "nt":  # take ffmpeg / streamlink / TwitchDownloaderCLI children down too
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.proc.kill()

    def refresh(self) -> None:
        """Read events the run appended since last time."""
        try:
            with open(self.events_path, "rb") as f:
                f.seek(self._ev_offset)
                chunk = f.read()
        except FileNotFoundError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        self._ev_offset += end + 1
        for raw in chunk[:end].split(b"\n"):
            try:
                ev = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            self.events.append(ev)
            if ev["type"] == "session" and ev.get("name") not in self.sessions:
                self.sessions.append(ev["name"])
            elif ev["type"] == "state":
                self.state = ev.get("state", self.state)
            elif ev["type"] == "stage":
                self.stage = ev.get("text", "")

    def summary(self) -> dict:
        self.refresh()
        return {"id": self.id, "mode": self.mode, "title": self.title, "argv": self.argv,
                "command": "clipper " + " ".join(_quote(a) for a in self.argv[:-2]),  # minus --out
                "running": self.running, "returncode": self.returncode, "state": self.state,
                "stage": self.stage, "stop_requested": self.stop_requested,
                "started": self.started, "ended": self.ended, "sessions": self.sessions}

    def snapshot(self, log_from: int, ev_from: int) -> dict:
        s = self.summary()
        with self._lock:
            s["log"] = self.log[log_from:]
            s["log_next"] = len(self.log)
        s["events"] = self.events[ev_from:]
        s["ev_next"] = len(self.events)
        return s


def _quote(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a


# --------------------------------------------------------------------------- building commands
def _parser_actions(mode: str):
    from ..cli import build_live_parser, build_parser
    p = build_live_parser() if mode == "live" else build_parser()
    return [a for a in p._actions if a.option_strings and a.dest not in _BLOCKED_DESTS]


def defaults(mode: str) -> dict:
    out = {}
    for a in _parser_actions(mode):
        out[a.dest] = a.default
    return out


def build_argv(mode: str, payload: dict) -> tuple[list[str], str]:
    """Turn the page's form into clipper arguments, accepting only known options."""
    opts = payload.get("options") or {}
    argv: list[str] = []
    if mode == "live":
        from ..live import channel_name
        channel = channel_name(str(payload.get("channel", "")))
        argv += ["live", channel]
        title = f"Live: {channel}"
    else:
        inputs = payload.get("inputs") or []
        if isinstance(inputs, str):
            inputs = inputs.split()
        ids = [parse_vod_id(str(x)) for x in inputs if str(x).strip()]
        if not ids:
            raise ApiError(400, "Paste a Twitch VOD URL first.")
        argv += ids
        what = "preview" if opts.get("dry_run") else "cut"
        title = f"VOD {ids[0]}" + (f" +{len(ids) - 1}" if len(ids) > 1 else "") + f" · {what}"

    for a in _parser_actions(mode):
        if a.dest not in opts:
            continue
        v = opts[a.dest]
        flag = a.option_strings[-1]
        if a.nargs == 0:                                   # store_true
            if bool(v) and not a.default:
                argv.append(flag)
            continue
        if v is None or v == "":
            continue
        if a.choices:
            if v not in a.choices:
                raise ApiError(400, f"{flag}: must be one of {', '.join(a.choices)}")
        elif a.type in (int, float):
            try:
                v = a.type(v)
            except (TypeError, ValueError):
                raise ApiError(400, f"{flag}: needs a number") from None
            if v < 0:
                raise ApiError(400, f"{flag}: can't be negative")
        elif a.type is None:
            v = str(v)
            if not _SAFE_STR.match(v):
                raise ApiError(400, f"{flag}: unexpected characters")
        else:
            continue                                       # paths etc. are never taken from the page
        if v == a.default:
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        argv += [flag, str(v)]
    return argv, title


# --------------------------------------------------------------------------- facecam helpers
def parse_time(s: str) -> float:
    s = str(s).strip().lower()
    if not s:
        return 0.0
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", s)
    if m and any(m.groups()):
        h, mi, se = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0)
    parts = s.split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        raise ApiError(400, f"Can't read the time {s!r}. Use 1:23:45 or 1h23m45s.") from None
    t = 0.0
    for n in nums:
        t = t * 60 + n
    return t


def save_facecams(path: Path, streamer: str, boxes: list[dict], ref_height: int) -> None:
    """Replace (or add) one streamer's block in streamers.yaml, leaving the rest of the file,
    comments included, untouched."""
    key = streamer.lower()
    with open(path, encoding="utf-8", newline="") as f:
        original = f.read()
    lines = original.splitlines()
    block = []
    if boxes:
        block = [f"  {key}:", "    facecams:"]
        for b in boxes:
            block.append(f"      - {{ x: {b['x']}, y: {b['y']}, w: {b['w']}, h: {b['h']}, ref_height: {ref_height} }}")

    si = next((i for i, l in enumerate(lines) if re.match(r"^streamers:\s*(#.*)?$", l)), None)
    if si is None:
        lines += ["", "streamers:"]
        si = len(lines) - 1
    sec_end = len(lines)
    start = None
    for i in range(si + 1, len(lines)):
        l = lines[i]
        if l.strip() and not l[0].isspace() and not l.startswith("#"):
            sec_end = i
            break
        if start is None and re.match(rf"^  {re.escape(key)}:\s*(#.*)?$", l):
            start = i
    if start is not None:
        end = start + 1
        while end < sec_end and (not lines[end].strip() or lines[end].startswith("   ")):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines[start:end] = block
    elif block:
        ins = sec_end
        while ins > si + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines[ins:ins] = ["", *block]
    while lines and not lines[-1].strip():
        lines.pop()
    nl = "\r\n" if "\r\n" in original else "\n"   # keep the file's own line endings
    _write_exact(path, nl.join(lines) + nl)
    try:
        config.load_facecams(key, path)
    except Exception:
        _write_exact(path, original)
        raise


def _write_exact(path: Path, text: str) -> None:
    """Write text without Windows newline translation."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# --------------------------------------------------------------------------- app
class App:
    def __init__(self, out_dir: Path, cwd: Path):
        self.out_dir = out_dir
        self.cwd = cwd
        self.work = Path(tempfile.mkdtemp(prefix="clipper-ui-"))
        self.token = secrets.token_urlsafe(24)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.allowed_hosts: set[str] = set()

    # sessions -----------------------------------------------------------
    def session_dir(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ApiError(400, "bad session name")
        d = self.out_dir / name
        if not d.is_dir():
            raise ApiError(404, f"no such session: {name}")
        return d

    def sessions(self) -> list[dict]:
        out = []
        if not self.out_dir.is_dir():
            return out
        for d in self.out_dir.iterdir():
            rp = d / "report.json"
            if not (d.is_dir() and rp.exists()):
                continue
            try:
                r = json.loads(rp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            clips = r.get("clips") or []
            out.append({"name": d.name, "mode": "live" if d.name.startswith("live_") else "vod",
                        "streamer": r.get("streamer"), "title": r.get("title"),
                        "length": r.get("vod_length"), "moments": len(clips),
                        "clips": sum(1 for c in clips if c.get("file") and (d / c["file"]).exists()),
                        "mtime": rp.stat().st_mtime})
        out.sort(key=lambda s: s["mtime"], reverse=True)
        return out

    def session(self, name: str) -> dict:
        d = self.session_dir(name)
        def load(fn):
            p = d / fn
            if not p.exists():
                return None
            for _ in range(5):   # the run may be replacing it right now
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.05)
            return None
        files = {p.name: p.stat().st_mtime for p in d.glob("*.mp4") if not p.name.endswith(".partial.mp4")}
        subs = sorted(p.name for p in d.glob("*.srt"))
        return {"name": name, "dir": str(d), "mode": "live" if name.startswith("live_") else "vod",
                "report": load("report.json"), "activity": load("activity.json"), "files": files, "subs": subs}

    # jobs -----------------------------------------------------------------
    def start_job(self, payload: dict) -> Job:
        mode = payload.get("mode")
        if mode == "captions":
            argv, title = self.captions_argv(payload)
        elif mode in ("vod", "live"):
            argv, title = build_argv(mode, payload)
        else:
            raise ApiError(400, "mode must be vod, live or captions")
        argv += ["--out", str(self.out_dir)]
        job = Job(mode, argv, title, self.work)
        with self.lock:
            self.jobs[job.id] = job
        job.start(self.cwd)
        return job

    def captions_argv(self, payload: dict) -> tuple[list[str], str]:
        name = str(payload.get("session", ""))
        self.session_dir(name)                     # must be one of our runs
        if any(j.running and name in j.sessions for j in self.jobs.values()):
            raise ApiError(409, "That run is still busy. Wait for it to finish first.")
        if not captions.available():
            raise ApiError(400, "Captions need faster-whisper:  " + captions.INSTALL)
        argv = ["captions", name]
        only = payload.get("only")
        if only:
            if not re.fullmatch(r"\d{1,3}(,\d{1,3})*", str(only)):
                raise ApiError(400, "only: clip ranks like 1,3,4")
            argv += ["--only", str(only)]
        if payload.get("retranscribe"):
            argv.append("--retranscribe")
        return argv, f"Captions: {name}"

    def captions_info(self) -> dict:
        return {"available": captions.available(), "install": captions.INSTALL,
                "config": asdict(captions.load_config()), "models": captions.MODELS,
                "positions": captions.POSITIONS, "gpu": captions.gpu_status(),
                "path": str(config.DEFAULT_DIR / "captions.yaml")}

    def save_captions(self, body: dict) -> dict:
        data = body.get("config")
        if not isinstance(data, dict):
            raise ApiError(400, "config missing")
        known = set(captions.CaptionConfig.__dataclass_fields__)
        cfg = captions.CaptionConfig(**{k: v for k, v in data.items() if k in known})
        if not isinstance(cfg.vocabulary, list) or not isinstance(cfg.replace, dict) or not isinstance(cfg.swears, list):
            raise ApiError(400, "vocabulary and swears must be lists, replace a mapping")
        captions.save_config(cfg)
        return self.captions_info()

    def job(self, jid: str) -> Job:
        j = self.jobs.get(jid)
        if not j:
            raise ApiError(404, "no such job (the dashboard was restarted?)")
        return j

    # tools ----------------------------------------------------------------
    def info(self) -> dict:
        found = {t: tools.find(t) for t in tools.TOOLS}
        import importlib.util
        has_mpl = importlib.util.find_spec("matplotlib") is not None
        return {"version": __version__, "out_dir": str(self.out_dir), "tools": found,
                "install": {t: s["install"] for t, s in tools.TOOLS.items()},
                "opencv": facecam.available(), "matplotlib": has_mpl, "captions": captions.available(),
                "oauth": bool(os.environ.get("TWITCH_OAUTH")),
                "defaults": {"vod": defaults("vod"), "live": defaults("live")}}

    # facecam frames ------------------------------------------------------------
    def grab_frame(self, payload: dict) -> str:
        ffmpeg = tools.find("ffmpeg")
        if not ffmpeg:
            raise ApiError(400, "ffmpeg isn't installed.")
        fid = uuid.uuid4().hex[:10]
        fdir = self.work / "frames"
        fdir.mkdir(exist_ok=True)
        jpg = fdir / f"{fid}.jpg"
        if payload.get("source") == "live":
            from ..live import channel_name
            sl = tools.find("streamlink")
            if not sl:
                raise ApiError(400, "streamlink isn't installed (pip install streamlink).")
            ch = channel_name(str(payload.get("channel", "")))
            src = subprocess.Popen([sl, "--stdout", "--loglevel", "error", f"twitch.tv/{ch}", "best"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
            try:
                ff = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0",
                                     "-frames:v", "1", "-q:v", "3", str(jpg)],
                                    stdin=src.stdout, capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                raise ApiError(504, f"Timed out getting a frame from twitch.tv/{ch}.") from None
            finally:
                src.kill()
                err = src.stderr.read().decode("utf-8", "replace") if src.stderr else ""
            if not jpg.exists():
                msg = err.strip().splitlines()[-1] if err.strip() else ff.stderr.decode("utf-8", "replace")[-300:]
                raise ApiError(400, f"Couldn't get a frame from twitch.tv/{ch}. Is it live? {msg}")
        else:
            from .. import video
            vod_id = parse_vod_id(str(payload.get("vod", "")))
            t = parse_time(payload.get("time", "0"))
            found = {k: tools.find(k) for k in tools.TOOLS}
            clip = fdir / f"{fid}.mp4"
            video.download_segment(vod_id, t, t + 4, clip, found, oauth=os.environ.get("TWITCH_OAUTH"))
            tools.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "1", "-i", str(clip),
                       "-frames:v", "1", "-q:v", "3", str(jpg)], "Grabbing a frame")
            clip.unlink(missing_ok=True)
        return f"/frames/{fid}.jpg"


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "clipper-ui"
    protocol_version = "HTTP/1.1"
    app: App

    def log_message(self, fmt, *args):  # keep the terminal quiet
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    # helpers --------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise ApiError(400, "bad JSON") from None

    def _file(self, path: Path) -> None:
        """Serve a file with Range support so videos can be seeked."""
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                    return
                left -= len(chunk)

    # routing --------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        app = self.app
        if self.headers.get("Host", "") not in app.allowed_hosts:
            self._send(403, b"forbidden", "text/plain")
            return
        url = urlparse(self.path)
        path = unquote(url.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if method != "GET" and not secrets.compare_digest(self.headers.get("X-Clipper-Token", ""), app.token):
                raise ApiError(403, "missing or wrong token; reload the page")
            self._route(method, path, q)
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except ClipperError as e:
            self._json({"error": str(e)}, 400)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:  # pragma: no cover - surfaced to the page and the terminal
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _route(self, method: str, path: str, q: dict) -> None:
        app = self.app
        if method == "GET" and path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__CLIPPER_TOKEN__", app.token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if method == "GET" and path.startswith("/static/"):
            p = (STATIC / path[len("/static/"):]).resolve()
            if not p.is_relative_to(STATIC.resolve()) or not p.is_file():
                raise ApiError(404, "not found")
            self._file(p)
            return
        if method == "GET" and path.startswith("/media/"):
            parts = path[len("/media/"):].split("/")
            if not (len(parts) == 2 or (len(parts) == 3 and parts[1] == "clean")):   # clean/: clips without captions
                raise ApiError(404, "not found")
            d = app.session_dir(parts[0])
            p = d.joinpath(*parts[1:]).resolve()
            if not p.is_relative_to(d.resolve()) or not p.is_file():
                raise ApiError(404, "not found")
            self._file(p)
            return
        if method == "GET" and path.startswith("/frames/"):
            name = path[len("/frames/"):]
            p = app.work / "frames" / name
            if not re.fullmatch(r"[0-9a-f]{10}\.jpg", name) or not p.is_file():
                raise ApiError(404, "not found")
            self._file(p)
            return

        if path == "/api/info" and method == "GET":
            self._json(app.info())
        elif path == "/api/sessions" and method == "GET":
            self._json(app.sessions())
        elif path == "/api/session" and method == "GET":
            self._json(app.session(q.get("name", "")))
        elif path == "/api/open" and method == "POST":
            name = self._body().get("name")
            _open_folder(app.session_dir(name) if name else app.out_dir)
            self._json({"ok": True})
        elif path == "/api/jobs" and method == "GET":
            self._json([j.summary() for j in sorted(app.jobs.values(), key=lambda j: -j.started)])
        elif path == "/api/jobs" and method == "POST":
            job = app.start_job(self._body())
            self._json(job.summary())
        elif (m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)) and method == "GET":
            self._json(app.job(m.group(1)).snapshot(int(q.get("log", 0)), int(q.get("ev", 0))))
        elif (m := re.fullmatch(r"/api/jobs/([0-9a-f]+)/(stop|kill)", path)) and method == "POST":
            job = app.job(m.group(1))
            job.stop() if m.group(2) == "stop" else job.kill()
            self._json(job.summary())
        elif (m := re.fullmatch(r"/api/config/([\w.]+)", path)):
            name = m.group(1)
            if name not in CONFIG_FILES:
                raise ApiError(404, "unknown config file")
            p = config.DEFAULT_DIR / name
            if method == "GET":
                self._json({"name": name, "path": str(p), "text": p.read_text(encoding="utf-8")})
            elif method == "PUT":
                self._json(_save_config(name, p, str(self._body().get("text", ""))))
            else:
                raise ApiError(405, "method not allowed")
        elif path == "/api/facecams" and method == "GET":
            streamer = q.get("streamer", "").strip().lower()
            self._json({"streamer": streamer, "boxes": config.load_facecams(streamer) if streamer else []})
        elif path == "/api/facecams" and method == "POST":
            b = self._body()
            streamer = str(b.get("streamer", "")).strip().lower()
            if not re.fullmatch(r"[a-z0-9_]{1,40}", streamer):
                raise ApiError(400, "Streamer name: letters, numbers and _ only.")
            try:
                boxes = [{k: int(round(float(bx[k]))) for k in ("x", "y", "w", "h")} for bx in b.get("boxes", [])]
                ref = int(b.get("ref_height") or 1080)
            except (KeyError, TypeError, ValueError):
                raise ApiError(400, "Each box needs numeric x, y, w, h.") from None
            if any(bx["w"] < 8 or bx["h"] < 8 for bx in boxes):
                raise ApiError(400, "A box is too small.")
            save_facecams(config.DEFAULT_DIR / "streamers.yaml", streamer, boxes, ref)
            self._json({"streamer": streamer, "boxes": config.load_facecams(streamer)})
        elif path == "/api/frame" and method == "POST":
            self._json({"url": app.grab_frame(self._body())})
        elif path == "/api/captions" and method == "GET":
            self._json(app.captions_info())
        elif path == "/api/captions" and method == "POST":
            self._json(app.save_captions(self._body()))
        else:
            raise ApiError(404, "not found")


def _save_config(name: str, path: Path, text: str) -> dict:
    """Validate by loading it the same way a run would, then save (keeping one backup)."""
    tmp = path.with_suffix(".check.yaml")
    _write_exact(tmp, text)
    try:
        if name == "keywords.yaml":
            config.load_scoring(tmp)
        elif name == "filters.yaml":
            config.load_filters(tmp)
        else:
            import yaml
            data = yaml.safe_load(text) or {}
            for key in (data.get("streamers") or {}):
                config.load_facecams(str(key), tmp)
    except ClipperError as e:
        raise ApiError(400, str(e)) from None
    except Exception as e:
        raise ApiError(400, f"Not saved: {type(e).__name__}: {e}") from None
    finally:
        tmp.unlink(missing_ok=True)
    shutil.copyfile(path, path.with_suffix(".yaml.bak"))
    _write_exact(path, text)
    return {"ok": True, "backup": str(path.with_suffix(".yaml.bak"))}


def _open_folder(d: Path) -> None:
    if os.name == "nt":
        os.startfile(str(d))  # noqa: S606 - local folder chosen from our own list
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(d)])
    else:
        subprocess.Popen(["xdg-open", str(d)])


# --------------------------------------------------------------------------- entry point
class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's already in use, so an
    # old dashboard and a new one could share it. Fail instead and move on to the next port.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # Browsers drop idle keep-alive connections all the time; that's not an error.
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clipper ui", description="Open the Chat Spike Clipper dashboard in your browser.")
    ap.add_argument("--port", type=int, default=8765, help="Port on 127.0.0.1 (the next free one is used if taken)")
    ap.add_argument("--out", default="./clips", help="Folder where runs are saved")
    ap.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    args = ap.parse_args(argv)

    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(out_dir, Path.cwd())
    handler = type("BoundHandler", (Handler,), {"app": app})

    httpd = None
    for port in range(args.port, args.port + 20):
        try:
            httpd = Server(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"error: no free port between {args.port} and {args.port + 19}", file=sys.stderr)
        return 2
    port =httpd.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    url = f"http://127.0.0.1:{port}/"
    print(f"Chat Spike Clipper dashboard: {url}")
    print(f"Clips are saved in {out_dir}")
    print("Keep this window open while you use it. Ctrl-C here stops the dashboard and any runs.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nstopping dashboard...")
    finally:
        httpd.server_close()
        for j in app.jobs.values():
            if j.running:
                j.stop()
        deadline = time.time() + 20
        while any(j.running for j in app.jobs.values()) and time.time() < deadline:
            time.sleep(0.3)
        for j in app.jobs.values():
            j.kill()
        shutil.rmtree(app.work, ignore_errors=True)
    return 0
