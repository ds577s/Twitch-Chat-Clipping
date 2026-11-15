# Chat Spike Clipper

Finds the best moments in long Twitch VODs **without watching them**, using chat spikes
as the signal, and cuts them into vertical 1080×1920 clips.

```bash
clipper https://www.twitch.tv/videos/123456789 --out ./clips
```

> Only clip streams you have rights to: your own, streamers who gave permission, or paid
> clipping campaigns.

## Setup (Windows)

1. **Python 3.10+**: `winget install Python.Python.3.12` (then open a new terminal).
2. **ffmpeg**: `winget install Gyan.FFmpeg`
3. **TwitchDownloaderCLI**: download the CLI zip for your OS from
   https://github.com/lay295/TwitchDownloader/releases and put `TwitchDownloaderCLI.exe`
   in this repo's `bin\` folder (or anywhere on PATH). Tested with 1.56.5.
4. **yt-dlp** (optional fallback for video): `pip install yt-dlp`
5. Install the tool itself, from this folder. `plot` adds the `--plot` chart, and
   `facecam` adds OpenCV for picking the facecam position automatically:

```bash
pip install -e ".[plot,facecam]"
```

`clipper` is now a command. You can also run `python -m clipper ...`.

The tool checks for these binaries at startup and tells you what's missing.
It also looks in `bin\`, or in the paths you set in `CLIPPER_TWITCHDOWNLOADER`,
`CLIPPER_FFMPEG` and `CLIPPER_YTDLP`.

## Workflow

**1. Dry run first.** Downloads only the chat, prints the ranked moments, writes the report
and a chart:

```bash
clipper https://www.twitch.tv/videos/123456789 --dry-run --plot
```

Open a few `vod_link`s from `clips/<vod_id>/report.csv` to check the timestamps land on
real moments. Look at `chat_activity.png` to tune thresholds. Re-runs reuse the cached
`chat.json`, so tuning is instant.

**2. Cut the clips:**

```bash
clipper https://www.twitch.tv/videos/123456789
```

Only the clip segments are downloaded, never the full VOD.

**3. Stacked facecam layout:** add the streamer's facecam box to
`clipper/config/streamers.yaml`, then:

```bash
clipper https://www.twitch.tv/videos/123456789 --layout stacked
```

If the streamer has no entry, it falls back to center crop.

Streamers often move their facecam between scenes. List every position under
`facecams:` and the tool checks each clip for which box has a face in it, using
OpenCV (`pip install -e ".[facecam]"`). If no box shows a face, that clip falls back to
center crop. The report records which position each clip used.

**4. Batch mode:** put one URL per line in a text file (`#` comments are allowed):

```bash
clipper vods.txt --max-clips 10
```

## Output

```
clips/<vod_id>/
  chat.json                      # cached chat
  01_02h14m08s_score187.mp4      # rank_clipstart_peakscore
  02_00h46m45s_score152.mp4
  report.json                    # everything: params, cleaning stats, muted periods, clips
  report.csv                     # one row per clip, top chat messages, VOD link at the timestamp
  chat_activity.png              # with --plot
```

## How detection works

1. **Clean.** Drop bots (`filters.yaml`), `!commands`, cheers, sub/gift/raid notices,
   poll votes ("1", "2"), and the invisible characters chatters add to get around
   Twitch's duplicate-message filter. Raids (lots of "raid" messages) and gift bombs
   (5+ subs) **mute** the following 90s / 45s, so the chat flood they cause can't become
   a clip. The last 150s of the VOD are muted too, so outro goodbyes ("WW STREAM")
   don't become a clip.
2. **Score** each 10s window: every message counts 1 plus its best keyword weight
   (`keywords.yaml`), plus 1 point per unique chatter. By default only one message per
   user per window counts, so a single spammer can't fake a spike.
3. **Spike** = score ≥ 3× the median of the surrounding 5 minutes (and ≥ `--min-score`).
   Or use `--method zscore --z 2.5`. Because it's relative, quiet hours produce clips too.
4. **Merge** spikes within 30s, rank by peak score (or `--rank-by ratio` to favor
   moments far above their local baseline), and drop overlapping clips.
5. **Clip** = 25s before the spike to 10s after, kept to 15–60s.

## Tuning tips

| Problem | Try |
|---|---|
| Too few clips | `--multiplier 2.5` or `--min-score 5` |
| Too much junk | `--multiplier 4`, or add the spam phrase / bot to the configs |
| All clips from the busiest hour | `--rank-by ratio` |
| Clips start too late / miss the setup | `--pre-roll 35` |
| Spikes split in two | `--merge-gap 45` |

## Options

Run `clipper --help` for the full list. Main ones:

| Flag | Default | Purpose |
|---|---|---|
| `url` | required | VOD URL/ID, several of them, or a `.txt` list |
| `--out` | `./clips` | Output folder |
| `--window` | 10 | Bucket size (s) |
| `--method` | ratio | `ratio` or `zscore` |
| `--multiplier` | 3.0 | Spike threshold vs baseline |
| `--pre-roll` / `--post-roll` | 25 / 10 | Seconds around the spike |
| `--max-clips` | 15 | Top N moments |
| `--layout` | center | `center` or `stacked` |
| `--streamer` | auto | Key for facecam config |
| `--quality` | source | e.g. `1080p60`, `720p60` |
| `--dry-run` | off | Report only, no video |
| `--plot` | off | Save `chat_activity.png` |
| `--oauth` | `$TWITCH_OAUTH` | For sub-only VODs |
| `--chat-file` | | Use an existing chat JSON |

## Tests

```bash
python -m unittest discover tests
```

Uses a synthetic 3-hour chat with real moments plus bot spam, a single-user spammer,
a gift bomb, a raid, cheers and `!enter` floods, and checks that only the real moments come out.

## Not built yet

Live mode (Twitch IRC + streamlink rolling buffer), milestone 6 in the spec.
