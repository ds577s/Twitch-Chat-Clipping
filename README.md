# Chat Spike Clipper

Finds the best moments in long Twitch streams **without watching them**, using chat spikes
as the signal, and cuts them into vertical 1080×1920 clips. It works on past broadcasts
(VODs) and on live streams.

```bash
clipper ui                                         # dashboard in your browser (everything below, with buttons)
clipper https://www.twitch.tv/videos/123456789     # VOD: rank the whole stream, top 15 clips
clipper live somechannel                           # live: a clip ~1 minute after each spike
clipper live somechannel --vod-after               # both: live clips now, best-of from the VOD after
clipper https://www.twitch.tv/videos/123456789 --captions   # add word-by-word speech captions
```

> Only clip streams you have rights to: your own, streamers who gave permission, or paid
> clipping campaigns.

## Setup (Windows)

1. **Python 3.10+**: `winget install Python.Python.3.12` (then open a new terminal).
2. **ffmpeg**: `winget install Gyan.FFmpeg`
3. **TwitchDownloaderCLI**: download the CLI zip for your OS from
   https://github.com/lay295/TwitchDownloader/releases and put `TwitchDownloaderCLI.exe`
   in this repo's `bin\` folder (or anywhere on PATH). Tested with 1.56.5.
4. Install the tool itself, from this folder. `plot` adds the `--plot` chart, `facecam`
   adds OpenCV for picking the facecam position automatically, `live` adds
   streamlink and yt-dlp for live mode (yt-dlp is also the VOD downloader fallback), and
   `captions` adds faster-whisper for speech captions:

```bash
pip install -e ".[plot,facecam,live,captions]"
```

`clipper` is now a command. You can also run `python -m clipper ...`.

The tool checks for these binaries at startup and tells you what's missing.
It also looks in `bin\`, or in the paths you set in `CLIPPER_TWITCHDOWNLOADER`,
`CLIPPER_FFMPEG`, `CLIPPER_YTDLP` and `CLIPPER_STREAMLINK`.

## Dashboard

```bash
clipper ui
```

Opens a dashboard in your browser. Or double-click **`dashboard.bat`** in this folder, which
also works when `clipper` isn't found yet (a terminal opened before Python was installed
doesn't see it). Keep that window open while you use it; closing it (or Ctrl-C) stops the
dashboard and anything running. If you close the browser tab, the dashboard is still at
http://127.0.0.1:8765/ while the window is open.

- **VODs tab:** paste a VOD link, click **Preview moments** to rank them from chat only, look at
  the chat chart and the top messages for each moment, untick the ones you don't want, then
  **Cut selected**. Clips play right on the page. Changing settings and previewing again is
  instant, because the chat is cached.
- **Live tab:** type a channel and **Start watching**. You see the chat, a live chart, every
  spike (clipped or skipped, and why) and the clips as they're made. **Stop** finishes any
  clip in progress first. Reloading the page doesn't interrupt anything.
- **Captions tab:** caption style with a live preview (font, size, colors, words at a time,
  position, ALL CAPS, swear masking), the speech model and language, words to always spell
  right, and **Add captions** for clips you've already cut. The **Add captions** switch on the
  VODs and Live tabs captions clips as they're made.
- **Setup tab:** which tools are installed, a **facecam picker** (grab a frame from a VOD or a
  live channel, drag a box around the facecam, see a preview of the vertical clip, save), and
  an editor for the keyword and filter files.
- **Runs** (left): everything in your clips folder, live sessions and VODs, to look back at.

Each run the dashboard starts is a normal `clipper` command (shown under the run), so the
dashboard and the terminal always behave the same. It only listens on `127.0.0.1` (this PC);
other devices and websites can't use it. `clipper ui --port 9000` picks another port,
`--out D:\clips` another clips folder.

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

## Live mode

```bash
clipper live somechannel --layout stacked --plot
```

It waits for the channel to go live, then:
- reads chat anonymously (no Twitch account needed),
- records the stream into a rolling buffer (last 5 minutes on disk, older parts deleted),
- cuts and renders a clip **about a minute after** each spike, in the background, while it
  keeps watching,
- stops when the stream ends (or Ctrl-C, or `--stop-after 120` for two hours), finishing
  any clip in progress.

Every spike that passes gets clipped (there's no "top 15" live, because later moments
aren't known yet). Use `--max-per-hour 6` or `--max-clips 20` to cap it.

**Hybrid:** `--vod-after` runs VOD mode on the stream's VOD when the stream ends. It ranks
the whole stream and clips the best moments that weren't already clipped live. It needs
yt-dlp (to find the VOD) and TwitchDownloaderCLI, and the channel must save past broadcasts.
If you stop early with Ctrl-C it skips this; run VOD mode yourself later.

How live detection differs: each window is first compared to the **previous** 5 minutes.
When a spike ends, its peak is re-checked against a baseline **centred** on it, using the
chat that came after. That rejects sustained rises (a raid of new viewers, a new game, the
streamer coming back from AFK), which look like spikes at first but aren't moments. These
show in the console as `skipped: sustained rise`. The ratio method (default) is
recommended live; z-score is noisier with a trailing baseline.

Raids and gift bombs are muted the same as in VOD mode, using the tags live chat provides.
Goodbye messages ("WW STREAM", "gn") are filtered, because live mode can't know when the
stream is about to end.

If clips start too early or late, your video may be out of sync with chat. Streamlink uses
Twitch's low-latency mode, so this is usually a few seconds at most. `--video-delay 5`
shifts clips 5s later.

**Tuning live:** the live chat is saved as `chat.json` in the same format as VOD chat, so
you can replay it with different settings without waiting for another stream:

```bash
clipper 0 --chat-file clips/live_somechannel_<date>/chat.json --dry-run --plot --multiplier 2.5
```

(VOD mode mutes the last 150s as an outro, so very short sessions replay a bit differently.)

Live output:

```
clips/live_<channel>_<date>_<time>/
  01_21h14m33s_score187.mp4      # number_localclocktime_peakscore
  report.json / report.csv       # updated after every clip
  chat.json                      # the live chat, replayable in VOD mode
  chat_live.jsonl                # raw chat log, written as it arrives
  chat_activity.png              # with --plot
```

## Captions

```bash
clipper https://www.twitch.tv/videos/123456789 --captions
clipper live somechannel --captions
clipper captions 123456789            # add or restyle captions on clips already cut
```

Each clip's speech is transcribed **on this PC** with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) (free, no account, nothing
uploaded) and burned in TikTok-style: a few words at a time, bold with an outline, the word
being spoken highlighted. With the facecam layout they sit on the line between the facecam
and the gameplay. Each clip also gets a plain `.srt` subtitle file.

- **Install:** `pip install -e ".[captions]"`. The speech model downloads once, the first time
  it's used (the default `small` is about 480 MB).
- **Speed:** on a laptop CPU, the `small` model takes about 10–25 s for a 45 s clip,
  including the extra encode. An NVIDIA GPU is used when NVIDIA's CUDA 12 cuBLAS and cuDNN
  libraries are installed; otherwise the CPU is used.
- **Style, model, language, spelling:** `clipper/config/captions.yaml`, or the dashboard's
  Captions tab. `vocabulary` helps Whisper spell streamer names, games and emotes; `replace`
  fixes words it keeps getting wrong.
- **Restyling is cheap:** every captioned clip keeps a copy without captions and its
  transcript in `clean/`, so `clipper captions <run>` after a style change just re-draws
  the captions. It transcribes again only when the model, language or vocabulary changed (or
  with `--retranscribe`).
- **Language:** detected per clip by default. Set it when you know it, since loud game audio
  or a few words in another language can fool detection.
- If captions fail for a clip, the clip is kept without them and the report says why.

## Output

```
clips/<vod_id>/
  chat.json                      # cached chat
  01_02h14m08s_score187.mp4      # rank_clipstart_peakscore
  02_00h46m45s_score152.mp4
  report.json                    # everything: params, cleaning stats, muted periods, clips
  report.csv                     # one row per clip, top chat messages, VOD link at the timestamp
  activity.json                  # chat score per window (the dashboard's chart)
  chat_activity.png              # with --plot
  01_02h14m08s_score187.srt      # with --captions: subtitles
  clean/                         # with --captions: clips without captions, and transcripts
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
| `--only` | | Only cut these ranks from a dry run with the same settings, e.g. `1,3,4` |
| `--captions` | off | Burn speech captions into each clip (live mode too) |
| `--caption-model` / `--caption-language` | from `captions.yaml` | e.g. `base`, `medium`; `en`, `es` |

Live mode (`clipper live --help` for all): the same detection, boundary, layout and config
options, plus:

| Flag | Default | Purpose |
|---|---|---|
| `channel` | required | Channel name or URL |
| `--quality` | best | `best`, `1080p60`, `720p60`, ... |
| `--max-clips` / `--max-per-hour` | 0 (no limit) | Cap how many clips get made |
| `--buffer` | 300 | Seconds of video kept on disk |
| `--video-delay` | 0 | Shift clips later if video lags chat |
| `--stop-after` | 0 | Stop after N minutes |
| `--vod-after` | off | Run VOD mode when the stream ends, skipping moments already clipped |
| `--vod-max-clips` | 15 | Top N for `--vod-after` |

## Tests

```bash
python -m unittest discover tests
```

Uses a synthetic 3-hour chat with real moments plus bot spam, a single-user spammer,
a gift bomb, a raid, cheers and `!enter` floods, and checks that only the real moments come out.
The live tests replay the same kind of stream in real-time order (including a ramp-up and
a sudden lasting jump in chat that must not become clips), parse real Twitch chat lines,
and record, cut and clean up a rolling buffer with ffmpeg. The dashboard tests check that
only known options reach the command line, and that saving facecams leaves the rest of
`streamers.yaml` untouched byte for byte. The caption tests check how words are grouped, the
`.ass` / `.srt` timing and highlighting, the settings file, and burn captions into a
generated clip with ffmpeg.
