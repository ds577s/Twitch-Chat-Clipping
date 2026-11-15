"""Command-line entry point: clipper <VOD URL | ID | list.txt> [options]"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import ClipperError, __version__
from . import chat as chatmod
from . import config, facecam, report, tools, video
from .detect import DetectParams, detect


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clipper",
        description="Find the best moments in Twitch VODs using chat spikes, and cut vertical 9:16 clips.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("inputs", nargs="+", metavar="url",
                   help="Twitch VOD URL or ID. Several allowed, or a .txt file with one per line (batch mode).")
    p.add_argument("--out", default="./clips", help="Output folder")

    g = p.add_argument_group("detection")
    g.add_argument("--window", type=float, default=10, help="Bucket size in seconds")
    g.add_argument("--method", choices=["ratio", "zscore"], default="ratio",
                   help="ratio: score >= baseline median x multiplier. zscore: rolling z-score")
    g.add_argument("--multiplier", type=float, default=3.0, help="Spike threshold vs baseline (ratio method)")
    g.add_argument("--z", type=float, default=2.5, help="z-score threshold (zscore method)")
    g.add_argument("--baseline", type=float, default=300, help="Seconds of surrounding chat used for the baseline")
    g.add_argument("--min-score", type=float, default=10, help="A spike must also reach at least this score")
    g.add_argument("--merge-gap", type=float, default=30, help="Merge spikes closer than this many seconds")
    g.add_argument("--max-clips", type=int, default=15, help="Keep the top N moments")
    g.add_argument("--rank-by", choices=["score", "ratio"], default="score",
                   help="Rank by peak score, or by how far above its local baseline the spike is")
    g.add_argument("--per-user-cap", type=int, default=None,
                   help="Max messages counted per user per window (0 = unlimited). Default: keywords.yaml")

    g = p.add_argument_group("clip boundaries")
    g.add_argument("--pre-roll", type=float, default=25, help="Seconds before the spike")
    g.add_argument("--post-roll", type=float, default=10, help="Seconds after the spike")
    g.add_argument("--min-len", type=float, default=15, help="Minimum clip length (s)")
    g.add_argument("--max-len", type=float, default=60, help="Maximum clip length (s)")

    g = p.add_argument_group("video")
    g.add_argument("--layout", choices=["center", "stacked"], default="center",
                   help="center: center crop. stacked: facecam on top, gameplay below")
    g.add_argument("--streamer", default="auto", help="Key into streamers.yaml for the facecam box (auto = channel name)")
    g.add_argument("--quality", default=None, help="e.g. 1080p60, 720p60. Default: source")
    g.add_argument("--downloader", choices=["auto", "twitchdownloader", "yt-dlp"], default="auto",
                   help="Segment downloader (auto tries TwitchDownloaderCLI then yt-dlp)")
    g.add_argument("--keep-raw", action="store_true", help="Keep the unprocessed downloaded segments")
    g.add_argument("--oauth", default=os.environ.get("TWITCH_OAUTH"),
                   help="Twitch OAuth token for sub-only VODs (or set TWITCH_OAUTH)")

    g = p.add_argument_group("misc")
    g.add_argument("--dry-run", action="store_true", help="Only write the report; download no video")
    g.add_argument("--plot", action="store_true", help="Save chat_activity.png with spikes marked (needs matplotlib)")
    g.add_argument("--refresh-chat", action="store_true", help="Re-download chat even if cached")
    g.add_argument("--chat-file", type=Path, help="Use this local chat JSON instead of downloading (single VOD only)")
    g.add_argument("--keywords", type=Path, help="Custom keywords.yaml")
    g.add_argument("--filters", type=Path, help="Custom filters.yaml")
    g.add_argument("--streamers", type=Path, help="Custom streamers.yaml")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def expand_inputs(inputs: list[str]) -> list[str]:
    out: list[str] = []
    for item in inputs:
        path = Path(item)
        if path.suffix.lower() == ".txt" or (path.is_file() and not item.isdigit()):
            if not path.is_file():
                raise ClipperError(f"Batch file not found: {path}")
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append(line)
        else:
            out.append(item)
    return out


def process_vod(vod_input: str, args, scoring, filters, found: dict) -> Path:
    vod_id = chatmod.parse_vod_id(vod_input)
    vod_dir = Path(args.out) / vod_id
    vod_dir.mkdir(parents=True, exist_ok=True)

    # 1. Chat (cached)
    if args.chat_file:
        chat_path = args.chat_file
        print(f"[{vod_id}] using chat file {chat_path}")
    else:
        chat_path = vod_dir / "chat.json"
        if chat_path.exists() and not args.refresh_chat:
            print(f"[{vod_id}] using cached chat {chat_path}")
        else:
            if not found.get("twitchdownloader"):
                tools.require(["twitchdownloader"])
            print(f"[{vod_id}] downloading chat (this can take a few minutes for long VODs)...")
            chatmod.download_chat(vod_id, chat_path, found["twitchdownloader"], args.oauth)

    meta, comments = chatmod.load_chat(chat_path, vod_id)
    if not comments:
        raise ClipperError(f"[{vod_id}] chat file has no messages.")

    # 2-5. Clean, score, detect, bound
    params = DetectParams(
        window=args.window, method=args.method, multiplier=args.multiplier, z=args.z,
        baseline_span=args.baseline, min_score=args.min_score, merge_gap=args.merge_gap,
        pre_roll=args.pre_roll, post_roll=args.post_roll, min_len=args.min_len, max_len=args.max_len,
        max_clips=args.max_clips, rank_by=args.rank_by,
    )
    msgs, stats = chatmod.clean(comments, filters, params.window, meta.length)
    wins, moments = detect(msgs, meta.length or 0, scoring, params, stats.mutes)

    records = [report.moment_record(i, m, vod_id, None if args.dry_run else report.clip_filename(i, m))
               for i, m in enumerate(moments, 1)]
    report.print_summary(meta, stats, records)
    jpath, _ = report.write_reports(vod_dir, meta, params, stats, records)

    if args.plot:
        from .plot import save_plot
        png = save_plot(wins, moments, vod_dir / "chat_activity.png",
                        title=f"{meta.streamer or ''} {vod_id} — {args.method}".strip())
        print(f"[{vod_id}] plot saved: {png}")

    if args.dry_run:
        print(f"[{vod_id}] dry run: report at {jpath}")
        return vod_dir

    # 6-7. Download segments and render
    streamer = meta.streamer if args.streamer == "auto" else args.streamer
    candidates: list[dict] = []
    detect_cam = False
    if args.layout == "stacked":
        candidates = config.load_facecams(streamer, args.streamers)
        if not candidates:
            print(f"[{vod_id}] no facecam config for '{streamer}' in streamers.yaml; using center layout")
        elif facecam.available():
            detect_cam = True
        elif len(candidates) > 1:
            print(f"[{vod_id}] several facecam positions configured but OpenCV isn't installed, so the "
                  f"first one is used for every clip. pip install \"opencv-python-headless<5\" to auto-pick.")

    raw_dir = vod_dir / "raw"
    failed = 0
    for i, (m, rec) in enumerate(zip(moments, records), 1):
        dest = vod_dir / rec["file"]
        raw = raw_dir / f"{i:02d}_{int(m.clip_start)}-{int(m.clip_end)}.mp4"
        print(f"[{vod_id}] clip {i}/{len(moments)}: {rec['clip_start_hms']} ({rec['duration']:.0f}s)")
        try:
            if not (raw.exists() and raw.stat().st_size > 0):
                video.download_segment(vod_id, m.clip_start, m.clip_end, raw, found,
                                       downloader=args.downloader, quality=args.quality, oauth=args.oauth)
            cam = candidates[0] if candidates else None
            if detect_cam:
                idx, hits = facecam.pick(raw, candidates)
                cam = candidates[idx] if idx is not None else None
                rec["facecam"] = idx + 1 if idx is not None else None  # 1-based, like the console
                print("  facecam: " + (f"position {idx + 1} (face in {hits[idx]}/{facecam.SAMPLES} frames)"
                                        if idx is not None else "no face found in any box, using center crop"))
            layout = "stacked" if cam else "center"
            rec["layout"] = layout
            video.render_vertical(raw, dest, found["ffmpeg"], layout, cam)
            if not args.keep_raw:
                raw.unlink(missing_ok=True)
        except ClipperError as e:
            failed += 1
            rec["file"] = None
            rec["error"] = str(e).splitlines()[0]
            print(f"  ! {e}", file=sys.stderr)
    if raw_dir.exists() and not any(raw_dir.iterdir()):
        raw_dir.rmdir()

    report.write_reports(vod_dir, meta, params, stats, records)
    done = len(moments) - failed
    print(f"[{vod_id}] {done}/{len(moments)} clips written to {vod_dir}")
    if failed and not done:
        raise ClipperError(f"[{vod_id}] every clip failed; see errors above.")
    return vod_dir


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # emoji in chat on Windows consoles
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    try:
        vods = expand_inputs(args.inputs)
        if not vods:
            raise ClipperError("No VODs given.")
        if args.chat_file and len(vods) > 1:
            raise ClipperError("--chat-file only works with a single VOD.")

        scoring = config.load_scoring(args.keywords)
        if args.per_user_cap is not None:
            scoring.per_user_cap = args.per_user_cap
        filters = config.load_filters(args.filters)

        # Check tools up front so a batch doesn't fail halfway through.
        if args.dry_run:
            found = {t: tools.find(t) for t in tools.TOOLS}
            needs_chat = not args.chat_file and any(
                args.refresh_chat or not (Path(args.out) / chatmod.parse_vod_id(v) / "chat.json").exists()
                for v in vods)
            if needs_chat:
                found.update(tools.require(["twitchdownloader"]))
        else:
            dl = {"auto": ["twitchdownloader", "yt-dlp"], "twitchdownloader": ["twitchdownloader"],
                  "yt-dlp": ["yt-dlp"]}[args.downloader]
            found = tools.require(["ffmpeg"], any_of=dl)
            for t in tools.TOOLS:
                found.setdefault(t, tools.find(t))
    except ClipperError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    failures = 0
    for v in vods:
        try:
            process_vod(v, args, scoring, filters, found)
        except ClipperError as e:
            failures += 1
            print(f"error: {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
    if len(vods) > 1:
        print(f"\nBatch done: {len(vods) - failures}/{len(vods)} VODs processed.")
    return 1 if failures else 0
