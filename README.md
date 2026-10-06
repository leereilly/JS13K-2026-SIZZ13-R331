# JS13K Gameplay Montage

`make-montage.sh` builds a deterministic, web-shareable MP4 in the order of
the CSV rows. Each clip is normalized and shown full-screen (portrait and
square gameplay use a blurred full-frame background), with a branded lower
third, normalized audio, repeatable fade transitions, and audio crossfades.

## Requirements

- Python 3.10+
- FFmpeg and FFprobe with `libx264`, AAC, `overlay`, `xfade`, and
  `acrossfade`
- librsvg (`rsvg-convert`), used to safely render arbitrary CSV text and the
  configured logo into lower-thirds
- A readable TTF/OTF font and Octocat/logo image. PNG is most portable;
  ImageMagick-supported SVG also works.

On macOS with Homebrew:

```bash
brew install ffmpeg librsvg
```

## Usage

```bash
./make-montage.sh videos/ data.csv output.mp4 \
  --octocat assets/GitHub_Invertocat_White.svg \
  --font /System/Library/Fonts/Supplemental/Verdana.ttf
```

The asset paths may instead be supplied as `OCTOCAT_PATH` and `FONT_PATH`;
command-line flags take precedence. Run `./make-montage.sh --help` for output
size, frame rate, transition, quality, and encoder-preset options.

By default an existing output is rejected. Use `--force` to replace it.
`--keep-temp` preserves the output-adjacent `.montage-work-*` directory for
diagnostics. Without it, work files and partial output are removed on both
success and failure.

## CSV format

These headers are required:

```csv
Place,Game title,GitHub usernames with @ that created it
2nd,"A title, with a comma",maker
1st,Another Game,@creator
```

Parsing uses Python's CSV implementation, including correct handling for
quoted commas, quotes, Unicode, CRLF, and UTF-8 BOMs. Surrounding whitespace is
trimmed. CSV row order controls playback order; the leading positive number in
`Place` selects `<number>.mp4`. In the example, `2.mp4` plays before `1.mp4`.
A single leading `@` is added to usernames when needed.

## Output and media handling

Defaults are H.264/yuv420p at 1920×1080 and 30 fps, plus AAC 48 kHz stereo,
with `faststart` enabled. Landscape, portrait, resolution, and frame-rate
differences are normalized. Existing audio is resampled, converted to stereo,
and loudness-normalized; missing audio is replaced with silence so transitions
remain reliable.

Output dimensions must be positive even numbers. Transition duration must be
shorter than every adjacent clip. Errors identify validation, probe,
lower-third, segment, or combine stages and include the relevant file.

## Tests

```bash
python3 -m unittest -v
bash -n make-montage.sh
```

The integration test creates mixed landscape/portrait and audio/silent media.
Set `MONTAGE_TEST_FONT` when Verdana is not available.
