"""Finding and running the external binaries (TwitchDownloaderCLI, yt-dlp, ffmpeg)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import ClipperError

# Extra places to look besides PATH: ./bin next to the project, and ./bin in the cwd.
_PROJECT_BIN = Path(__file__).resolve().parent.parent / "bin"

TOOLS = {
    "twitchdownloader": {
        "names": ["TwitchDownloaderCLI"],
        "env": "CLIPPER_TWITCHDOWNLOADER",
        "install": (
            "TwitchDownloaderCLI: download the CLI zip for your OS from\n"
            "    https://github.com/lay295/TwitchDownloader/releases\n"
            "  and put TwitchDownloaderCLI(.exe) in this project's bin/ folder, on your PATH,\n"
            "  or point the CLIPPER_TWITCHDOWNLOADER environment variable at it."
        ),
    },
    "ffmpeg": {
        "names": ["ffmpeg"],
        "env": "CLIPPER_FFMPEG",
        "install": (
            "ffmpeg: Windows: winget install Gyan.FFmpeg   macOS: brew install ffmpeg\n"
            "  Linux: sudo apt install ffmpeg   (or drop ffmpeg(.exe) in this project's bin/ folder)"
        ),
    },
    "streamlink": {
        "names": ["streamlink"],
        "env": "CLIPPER_STREAMLINK",
        "install": "streamlink (live mode): pip install streamlink",
    },
    "yt-dlp": {
        "names": ["yt-dlp"],
        "env": "CLIPPER_YTDLP",
        "install": "yt-dlp: pip install yt-dlp   (or: winget install yt-dlp.yt-dlp / brew install yt-dlp)",
    },
}


def find(tool: str) -> str | None:
    spec = TOOLS[tool]
    env = os.environ.get(spec["env"])
    if env and Path(env).exists():
        return env
    for name in spec["names"]:
        for d in (_PROJECT_BIN, Path.cwd() / "bin"):
            for cand in (d / name, d / f"{name}.exe"):
                if cand.is_file():
                    return str(cand)
        found = shutil.which(name)
        if found:
            return found
    return None


def require(tools: list[str], any_of: list[str] | None = None) -> dict[str, str | None]:
    """Locate tools; raise a ClipperError with install steps if any required ones are missing.

    `tools` are all required. `any_of` needs at least one present.
    """
    found = {t: find(t) for t in set(tools) | set(any_of or [])}
    missing = [t for t in tools if not found[t]]
    if any_of and not any(found[t] for t in any_of):
        missing += [t for t in any_of if t not in missing]
        header = f"Need at least one of: {', '.join(any_of)}."
    else:
        header = ""
    if missing:
        lines = ["Missing required tools:", header] if header else ["Missing required tools:"]
        for t in missing:
            lines.append("  - " + TOOLS[t]["install"])
        raise ClipperError("\n".join(l for l in lines if l))
    return found


def run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    """Run a command, capturing output. Raises ClipperError with output on failure."""
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # TwitchDownloaderCLI may prompt on file collisions
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise ClipperError(f"{what}: could not run {cmd[0]} ({e})") from e
    if proc.returncode != 0:
        out = (proc.stderr or "") + "\n" + (proc.stdout or "")
        raise ClipperError(f"{what} failed (exit {proc.returncode}).\n{explain_twitch_error(out)}")
    return proc


def explain_twitch_error(output: str) -> str:
    """Turn raw downloader output into a friendlier hint, keeping the tail of the log."""
    low = output.lower()
    hint = ""
    if any(s in low for s in ("sub-only", "subscriber", "subscriber-only", "restricted", "unauthorized", "401", "403")):
        hint = ("This VOD looks subscriber-only or restricted. Pass --oauth <token> (or set TWITCH_OAUTH) "
                "from an account that can watch it.\n")
    elif any(s in low for s in ("deleted", "expired", "invalid vod", "not found", "404", "does not exist")):
        hint = "This VOD seems to be deleted, expired, or the ID is wrong.\n"
    tail = "\n".join(output.strip().splitlines()[-15:])
    return hint + "Last output:\n" + tail
