"""Steps 6-7: download only the needed VOD segments, then render 1080x1920 vertical clips."""

from __future__ import annotations

from pathlib import Path

from . import ClipperError
from . import tools

OUT_W, OUT_H = 1080, 1920
TOP_H = 640                    # facecam band in stacked layout (1/3)
BOTTOM_H = OUT_H - TOP_H       # gameplay band (2/3)


def download_segment(vod_id: str, start: float, end: float, dest: Path, found: dict,
                     downloader: str = "auto", quality: str | None = None,
                     oauth: str | None = None) -> None:
    """Download [start, end] seconds of a VOD to `dest`. Never fetches the whole VOD."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    order = {
        "auto": ["twitchdownloader", "yt-dlp"],
        "twitchdownloader": ["twitchdownloader"],
        "yt-dlp": ["yt-dlp"],
    }[downloader]
    errors = []
    for tool in order:
        exe = found.get(tool)
        if not exe:
            continue
        dest.unlink(missing_ok=True)  # TwitchDownloaderCLI prompts if the file exists
        try:
            if tool == "twitchdownloader":
                cmd = [exe, "videodownload", "--id", vod_id,
                       "--beginning", f"{int(start)}s", "--ending", f"{int(round(end))}s",
                       "-o", str(dest)]
                if quality:
                    cmd += ["-q", quality]
                if found.get("ffmpeg"):
                    cmd += ["--ffmpeg-path", found["ffmpeg"]]
                if oauth:
                    cmd += ["--oauth", oauth]
            else:
                cmd = [exe, "--quiet", "--no-warnings", "--no-part",
                       "--download-sections", f"*{start:.2f}-{end:.2f}",
                       "-f", _ytdlp_format(quality), "--merge-output-format", "mp4",
                       "-o", str(dest)]
                if found.get("ffmpeg"):
                    cmd += ["--ffmpeg-location", found["ffmpeg"]]
                cmd.append(f"https://www.twitch.tv/videos/{vod_id}")
            tools.run(cmd, f"Segment download via {tool}")
            if dest.exists() and dest.stat().st_size > 0:
                return
            errors.append(f"{tool}: finished but produced no file")
        except ClipperError as e:
            errors.append(str(e))
    if not errors:
        raise ClipperError("No video downloader available (need TwitchDownloaderCLI or yt-dlp).")
    raise ClipperError("Could not download segment:\n" + "\n\n".join(errors))


def _ytdlp_format(quality: str | None) -> str:
    if not quality or quality.lower() in ("source", "best"):
        return "best"
    digits = "".join(ch for ch in quality.split("p")[0] if ch.isdigit())
    return f"best[height<={digits}]/best" if digits else "best"


def build_filter(layout: str, facecam: dict | None) -> str:
    """ffmpeg filter graph producing [v] at 1080x1920."""
    # Crop width from source height, rounded to an even number of pixels.
    center = (f"[0:v]crop=trunc(ih*{OUT_W}/{OUT_H}/2)*2:ih,"
              f"scale={OUT_W}:{OUT_H},setsar=1[v]")
    if layout != "stacked" or not facecam:
        return center
    s = f"ih/{facecam['ref_height']:g}"  # scale config coords to the downloaded resolution
    fc = (f"crop=w={facecam['w']:g}*{s}:h={facecam['h']:g}*{s}"
          f":x={facecam['x']:g}*{s}:y={facecam['y']:g}*{s}")
    return (
        "[0:v]split=2[cam][game];"
        f"[cam]{fc},scale={OUT_W}:{TOP_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{TOP_H}[top];"
        f"[game]crop=trunc(ih*{OUT_W}/{BOTTOM_H}/2)*2:ih,scale={OUT_W}:{BOTTOM_H}[bottom];"
        "[top][bottom]vstack=inputs=2,setsar=1[v]"
    )


def render_vertical(src: Path, dest: Path, ffmpeg: str, layout: str = "center",
                    facecam: dict | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".partial.mp4")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src),
           "-filter_complex", build_filter(layout, facecam),
           "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart",
           str(tmp)]
    tools.run(cmd, f"Rendering {dest.name}")
    tmp.replace(dest)
