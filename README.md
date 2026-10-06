# JS13K Gameplay Montage

`make-montage.sh` builds a polished, web-shareable montage in CSV row order.
Gameplay is aspect-fit over a blurred extension, normalized to a constant frame
rate with 48 kHz stereo audio, and presented with animated GitHub-style lower
thirds. The exported timeline is a true video **and audio** cycle.

## Requirements

- Python 3.10+ and Pillow (`python3 -m pip install -r requirements.txt`)
- FFmpeg/FFprobe with `libx264`, AAC, `overlay`, `xfade`, and `acrossfade`
- Mona Sans regular and bold TTF/OTF files
- `rsvg-convert` when the configured Octocat is SVG

On macOS:

```bash
brew install ffmpeg librsvg
python3 -m pip install -r requirements.txt
```

## Usage

```bash
./make-montage.sh videos/ data.csv output.mp4 \
  --octocat assets/GitHub_Invertocat_White.svg \
  --font /Library/Fonts/MonaSans-Regular.ttf \
  --font-bold /Library/Fonts/MonaSans-Bold.ttf
```

The repository white Octocat and installed `/Library/Fonts/MonaSans-*.ttf`
paths are discovered by default. `OCTOCAT_PATH`, `FONT_PATH`, and
`FONT_BOLD_PATH` are also supported; explicit flags win.

Useful options:

- `--width`, `--height`, `--fps`, `--transition-duration` (default 0.65 s)
- `--loop-seam` (default 1.5 s into Game 1)
- `--avatar-cache`, `--avatar-timeout`, `--refresh-avatars`, `--offline`
- `--crf`, `--preset`, `--keep-temp`, `--force`, `--verbose`

`--force` applies to all four deliverables. Publication is atomic: existing
deliverables are left in place if rendering fails. `--keep-temp` preserves the
output-adjacent `.montage-work-*` directory; otherwise intermediates and
partial files are removed.

## CSV format

```csv
Place,Game title,GitHub usernames with @ that created it,Lower third position
2nd,"A title, with a comma",maker,bottom-right
1st,Another Game,@creator,bottom-left
```

The first three headers are required. `Lower third position` is optional and
accepts `bottom-left` (the default) or `bottom-right`. CSV order controls
playback; the leading placement number selects `<number>.mp4`, so filenames
are never guessed from titles. Placement values are not displayed in the
lower third. Usernames are validated and displayed with exactly one `@`.

## Lower thirds and avatars

Pillow composes a panel-sized RGBA graphic with a circular avatar, fitted
one- or two-line title, GitHub username, and configured Octocat branding. The
charcoal rounded panel has a soft shadow, subtle border and rightward fade,
plus a GitHub-green accent. It eases upward and fades in after the incoming
transition, then fades and eases down before the outgoing transition,
preventing two titles from overlapping.

Avatars are loaded cache-first from `https://github.com/USERNAME.png`.
Redirects are followed, downloads are bounded, and decoded image content is
validated before atomic caching. Corrupt cache entries are replaced online.
Any per-avatar download or validation failure emits an actionable warning and
uses initials without blocking montage generation. A valid cached avatar is
retained if a requested refresh fails. `--offline` performs no network access
and is fully reproducible with cached or fallback avatars. The Octocat is never
downloaded.

## Seamless cyclic timeline

Transitions are quantized once to whole frames (0.65 s becomes 20 frames at
30 fps). Audio uses a matching integer number of 48 kHz samples. The builder
crossfades:

```text
Game 1 → … → Game 13 → repeated Game 1
```

It then extracts exactly one cycle from the configured stable seam within
Game 1. Thus Game 1 occurs once, split over the file boundary. The final and
first frames—and corresponding audio samples—are consecutive Game 1 moments,
while the Game 13 → Game 1 crossfade remains inside the file. Missing source
audio is replaced by silence; existing audio is resampled, stereo-converted,
and loudness-normalized.

FPS must divide 48,000 for sample-exact boundaries. Clip lengths are probed and
frame aligned; unequal lengths are supported and playback speed is unchanged.

## Deliverables

Created beside the requested output:

- `output.mp4` (or the requested filename): H.264/yuv420p, AAC, fast-start
- `lower-third-preview.png`: actual Game 1 gameplay and composed overlay
- `montage-contact-sheet.jpg`: 13 ordered representative gameplay frames
- `loop-test.mp4`: decoded final ~2 seconds followed directly by first ~2
  seconds, with no added fade at its midpoint

Inspect the contact sheet for title/HUD placement and play `loop-test.mp4`
repeatedly with audio to assess the actual wraparound.

## Tests

```bash
python3 -m unittest -v
bash -n make-montage.sh
```

Tests cover CSV parsing and positions, cache/fallback behavior, graphics
bounds, frame/sample cycle math, filter construction, mixed aspect ratios,
missing audio, artifact generation, and media stream properties.
