#!/usr/bin/env python3
"""Reproducible, cyclic FFmpeg gameplay montage builder."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Literal, Sequence

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, UnidentifiedImageError
except ImportError as error:  # pragma: no cover - exercised by CLI installations
    raise SystemExit("Pillow is required; run: python3 -m pip install -r requirements.txt") from error


PLACE_HEADER = "Place"
TITLE_HEADER = "Game title"
USER_HEADER = "GitHub usernames with @ that created it"
POSITION_HEADER = "Lower third position"
REQUIRED_HEADERS = (PLACE_HEADER, TITLE_HEADER, USER_HEADER)
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
SAMPLE_RATE = 48_000


class MontageError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameRow:
    row_number: int
    place: str
    place_number: int
    title: str
    username: str
    clip_path: Path
    github_login: str = ""
    position: Literal["bottom-left", "bottom-right"] = "bottom-left"


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: Decimal
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    frame_rate: Fraction | None
    frame_count: int | None = None
    sample_rate: int | None = None
    channels: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None


@dataclass(frozen=True)
class MontageConfig:
    input_dir: Path
    csv_path: Path
    output_path: Path
    octocat_path: Path
    font_path: Path
    width: int
    height: int
    fps: int
    transition_duration: Decimal
    keep_temp: bool
    force: bool
    crf: int = 20
    preset: str = "medium"
    verbose: bool = False
    avatar_cache: Path | None = None
    avatar_timeout: float = 8.0
    refresh_avatars: bool = False
    offline: bool = False
    loop_seam: Decimal = Decimal("1.5")
    font_bold_path: Path | None = None
    instagram_background: Path | None = None
    instagram_output: Path | None = None
    hubot_font_path: Path | None = None
    instagram_width: int = 1080
    instagram_height: int = 1920

    @property
    def preview_path(self) -> Path:
        return self.output_path.parent / "lower-third-preview.png"

    @property
    def instagram_enabled(self) -> bool:
        return self.instagram_background is not None

    @property
    def instagram_output_path(self) -> Path:
        if self.instagram_output is not None:
            return self.instagram_output
        base = self.output_path
        return base.with_name(f"{base.stem}-instagram{base.suffix}")

    @property
    def contact_sheet_path(self) -> Path:
        return self.output_path.parent / "montage-contact-sheet.jpg"

    @property
    def loop_test_path(self) -> Path:
        return self.output_path.parent / "loop-test.mp4"

    @property
    def cache_path(self) -> Path:
        return self.avatar_cache or self.output_path.parent / ".avatar-cache"


@dataclass(frozen=True)
class LowerThirdLayout:
    width: int
    height: int
    x: int
    y: int
    title_lines: tuple[str, ...]
    title_size: int
    avatar_bounds: tuple[int, int, int, int]
    title_bounds: tuple[int, int, int, int]
    username_bounds: tuple[int, int, int, int]
    octocat_bounds: tuple[int, int, int, int]


@dataclass(frozen=True)
class InstagramLayout:
    width: int
    height: int
    panel_height: int
    background_bounds: tuple[int, int, int, int]
    place_bounds: tuple[int, int, int, int]
    title_bounds: tuple[int, int, int, int]
    avatar_bounds: tuple[int, int, int, int]
    creator_bounds: tuple[int, int, int, int]
    wordmark_bounds: tuple[int, int, int, int]
    octocat_bounds: tuple[int, int, int, int]
    title_lines: tuple[str, ...]
    title_size: int


@dataclass(frozen=True)
class CycleTiming:
    frame_counts: tuple[int, ...]
    transition_frames: int
    transition: Decimal
    seam_frame: int
    cycle_frames: int
    samples_per_frame: int

    @property
    def cycle_samples(self) -> int:
        return self.cycle_frames * self.samples_per_frame


def parse_place_number(value: str) -> int:
    match = re.match(r"^\s*([1-9][0-9]*)", value or "")
    if not match:
        raise ValueError(f"placement must start with a positive integer: {value!r}")
    return int(match.group(1))


def bare_login(value: str) -> str:
    login = (value or "").strip().lstrip("@")
    if not login:
        raise ValueError("username is blank")
    if not LOGIN_RE.fullmatch(login):
        raise ValueError(f"invalid GitHub username: {value!r}")
    return login


def normalize_username(value: str) -> str:
    return f"@{bare_login(value)}"


def parse_csv(path: Path, input_dir: Path) -> list[GameRow]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise ValueError(f"cannot read CSV {path}: {error}") from error
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV {path} is empty")
        headers = {(name or "").strip(): name for name in reader.fieldnames}
        missing = [name for name in REQUIRED_HEADERS if name not in headers]
        if missing:
            raise ValueError(f"CSV missing required header(s): {', '.join(missing)}")
        rows: list[GameRow] = []
        placements: set[int] = set()
        for logical_row, raw in enumerate(reader, start=2):
            place = (raw.get(headers[PLACE_HEADER]) or "").strip()
            title = (raw.get(headers[TITLE_HEADER]) or "").strip()
            user = (raw.get(headers[USER_HEADER]) or "").strip()
            if not any((place, title, user)):
                continue
            if not all((place, title, user)):
                raise ValueError(f"CSV row {logical_row} has a blank required field")
            try:
                place_number = parse_place_number(place)
                login = bare_login(user)
            except ValueError as error:
                raise ValueError(f"CSV row {logical_row}: {error}") from error
            position = (raw.get(headers.get(POSITION_HEADER, "")) or "bottom-left").strip().lower()
            if position not in ("bottom-left", "bottom-right"):
                raise ValueError(
                    f"CSV row {logical_row}: {POSITION_HEADER} must be bottom-left or bottom-right"
                )
            if place_number in placements:
                raise ValueError(f"CSV row {logical_row}: duplicate placement {place_number}")
            clip = input_dir / f"{place_number}.mp4"
            if not clip.is_file():
                raise ValueError(f"CSV row {logical_row}: missing clip {clip}")
            placements.add(place_number)
            rows.append(GameRow(logical_row, place, place_number, title, f"@{login}", clip, login, position))
    if not rows:
        raise ValueError("CSV contains no game rows")
    return rows


def _positive_decimal(value: object, description: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MontageError(f"invalid {description}: {value!r}") from None
    if not result.is_finite() or result <= 0:
        raise MontageError(f"invalid {description}: {value!r}")
    return result


def probe_media(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    command = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        result = subprocess.run(command, text=True, capture_output=True)
    except FileNotFoundError as error:
        raise MontageError(f"ffprobe not found: {error}") from error
    if result.returncode:
        raise MontageError(f"probe failed for {path}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise MontageError(f"probe returned malformed JSON for {path}: {error}") from error
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MontageError(f"no video stream in {path}")
    duration = _positive_decimal(
        video.get("duration") or data.get("format", {}).get("duration"), f"duration for {path}"
    )
    rate_value = video.get("avg_frame_rate") or video.get("r_frame_rate")
    try:
        rate = Fraction(rate_value) if rate_value and rate_value != "0/0" else None
    except (ValueError, ZeroDivisionError):
        rate = None
    return MediaInfo(
        path, duration, True, audio is not None, video.get("width"), video.get("height"), rate,
        int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        int(audio["sample_rate"]) if audio and str(audio.get("sample_rate", "")).isdigit() else None,
        int(audio["channels"]) if audio and audio.get("channels") is not None else None,
        video.get("codec_name"), audio.get("codec_name") if audio else None,
    )


def calculate_transition_offsets(durations: Sequence[Decimal], transition: Decimal) -> list[Decimal]:
    if transition <= 0:
        raise ValueError("transition duration must be positive")
    for left, right in zip(durations, durations[1:]):
        if transition >= left or transition >= right:
            raise ValueError("transition duration must be shorter than every adjacent clip")
    return [sum(durations[:i], Decimal(0)) - transition * i for i in range(1, len(durations))]


def calculate_cycle_timing(
    frame_counts: Sequence[int], fps: int, requested_transition: Decimal, requested_seam: Decimal
) -> CycleTiming:
    if not frame_counts:
        raise ValueError("at least one clip is required")
    if fps <= 0 or SAMPLE_RATE % fps:
        raise ValueError("fps must be positive and divide 48000 for sample-accurate looping")
    transition_frames = int((requested_transition * fps).to_integral_value(rounding=ROUND_HALF_UP))
    seam_frame = int((requested_seam * fps).to_integral_value(rounding=ROUND_HALF_UP))
    if transition_frames <= 0:
        raise ValueError("transition rounds to zero frames")
    if any(n <= 2 * transition_frames for n in frame_counts):
        raise ValueError("every clip must be longer than twice the frame-aligned transition")
    entrance_end = transition_frames + int((Decimal("0.25") * fps).to_integral_value(rounding=ROUND_HALF_UP))
    if seam_frame < entrance_end or seam_frame >= frame_counts[0] - transition_frames:
        raise ValueError("loop seam must be after the first lower-third entrance and before its exit/transition")
    cycle_frames = sum(frame_counts) - len(frame_counts) * transition_frames
    return CycleTiming(
        tuple(frame_counts), transition_frames, Decimal(transition_frames) / fps,
        seam_frame, cycle_frames, SAMPLE_RATE // fps,
    )


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def run_command(argv: Sequence[str], stage: str, verbose: bool = False) -> None:
    if verbose:
        print(f"[{stage}] {' '.join(str(x) for x in argv)}", file=sys.stderr)
    try:
        result = subprocess.run(list(argv), text=True, capture_output=not verbose)
    except FileNotFoundError as error:
        raise MontageError(f"{stage}: dependency not found: {argv[0]}") from error
    if result.returncode:
        stderr = "" if verbose else (result.stderr or "")
        tail = "\n".join(stderr.strip().splitlines()[-20:])
        raise MontageError(f"{stage} failed with exit code {result.returncode}" + (f":\n{tail}" if tail else ""))


def avatar_cache_path(login: str, cache_dir: Path) -> Path:
    safe = bare_login(login).lower()
    digest = hashlib.sha256(safe.encode()).hexdigest()[:10]
    return cache_dir / f"{safe}-{digest}.png"


def _valid_image(data: bytes) -> None:
    if not data:
        raise ValueError("avatar response is empty")
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("avatar response exceeds 8 MiB")
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        if image.width < 1 or image.height < 1 or image.width > 8192 or image.height > 8192:
            raise ValueError("avatar dimensions are unsafe")
        image.convert("RGBA").load()


def fetch_avatar(login: str, config: MontageConfig) -> Path | None:
    login = bare_login(login)
    target = avatar_cache_path(login, config.cache_path)
    cached: Path | None = None
    if target.is_file():
        try:
            _valid_image(target.read_bytes())
            cached = target
            if not config.refresh_avatars:
                return cached
        except (OSError, ValueError, UnidentifiedImageError) as error:
            if config.offline:
                warnings.warn(
                    f"cached avatar for @{login} is invalid ({error}) and offline mode "
                    "prevents recovery; using initials fallback"
                )
                return None
            warnings.warn(f"cached avatar for @{login} is invalid ({error}); downloading a replacement")
    if config.offline:
        if cached is not None:
            return cached
        warnings.warn(f"avatar for @{login} is not cached and offline mode is enabled; using fallback")
        return None
    url = f"https://github.com/{login}.png?size=256"
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "js13k-montage/1.0 (+https://github.com/)",
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=config.avatar_timeout) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                raise ValueError(f"unexpected content type {content_type}")
            data = response.read(8 * 1024 * 1024 + 1)
        _valid_image(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        staging: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{target.name}.", suffix=".partial",
                dir=target.parent, delete=False,
            ) as handle:
                staging = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _valid_image(staging.read_bytes())
            os.replace(staging, target)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return target
    except Exception as error:
        detail = (
            f"HTTP {error.code} {error.reason}"
            if isinstance(error, urllib.error.HTTPError)
            else f"{type(error).__name__}: {error}"
        )
        fallback = "using cached avatar" if cached is not None else "using initials fallback"
        warnings.warn(f"avatar download for @{login} failed ({detail}); {fallback}")
        return cached


def fallback_initials(login: str) -> str:
    parts = [part for part in re.split(r"[-_.]+", bare_login(login)) if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    capitals = [c for c in parts[0][1:] if c.isupper()] if parts else []
    return ((parts[0][0] + capitals[0]) if parts and capitals else (parts[0][:2] if parts else "?")).upper()


def _center_crop(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGBA")
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)


def load_avatar_or_fallback(row: GameRow, config: MontageConfig, size: int) -> Image.Image:
    login = row.github_login or bare_login(row.username)
    path = fetch_avatar(login, config)
    canvas = Image.new("RGBA", (size, size))
    mask = Image.new("L", (size, size))
    ImageDraw.Draw(mask).ellipse((1, 1, size - 2, size - 2), fill=255)
    if path:
        try:
            with Image.open(path) as source:
                avatar = _center_crop(source, size)
            canvas.paste(avatar, (0, 0), mask)
        except (OSError, UnidentifiedImageError) as error:
            warnings.warn(f"could not decode cached avatar for @{login}: {error}; using initials fallback")
            path = None
    if not path:
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((1, 1, size - 2, size - 2), fill=(31, 42, 48, 255))
        font = ImageFont.truetype(str(config.font_bold_path or config.font_path), max(12, round(size * .36)))
        text = fallback_initials(login)
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - box[1]),
                  text, font=font, fill=(126, 231, 135, 255))
    ImageDraw.Draw(canvas).ellipse((1, 1, size - 2, size - 2), outline=(255, 255, 255, 100), width=max(1, size // 48))
    canvas.putalpha(Image.composite(canvas.getchannel("A"), Image.new("L", (size, size)), mask))
    return canvas


def _load_octocat(config: MontageConfig, size: int) -> Image.Image:
    path = config.octocat_path
    if path.suffix.lower() == ".svg":
        result = subprocess.run(
            ["rsvg-convert", "-w", str(size), "-h", str(size), str(path)],
            capture_output=True,
        )
        if result.returncode:
            raise MontageError(f"could not render Octocat SVG {path}: {result.stderr.decode(errors='replace')}")
        source = Image.open(io.BytesIO(result.stdout))
    else:
        source = Image.open(path)
    with source:
        return source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)


def _fit_title(draw: ImageDraw.ImageDraw, title: str, font_path: Path, max_width: int, max_height: int,
               start_size: int, minimum: int) -> tuple[ImageFont.FreeTypeFont, tuple[str, ...]]:
    words = title.split()
    for size in range(start_size, minimum - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        if draw.textbbox((0, 0), title, font=font)[2] <= max_width:
            return font, (title,)
        for cut in range(1, len(words)):
            lines = (" ".join(words[:cut]), " ".join(words[cut:]))
            widths = [draw.textbbox((0, 0), line, font=font)[2] for line in lines]
            line_height = draw.textbbox((0, 0), "Ag", font=font)[3]
            if max(widths) <= max_width and line_height * 2 + round(size * .12) <= max_height:
                return font, lines
    raise MontageError(f"game title cannot fit in lower third: {title!r}")


def render_lower_third(
    row: GameRow, avatar: Image.Image, config: MontageConfig, output_png: Path
) -> LowerThirdLayout:
    scale = config.height / 1080
    width = min(round(750 * scale), config.width - 2 * max(18, round(60 * scale)))
    height = min(round(150 * scale), int(config.height * .15))
    width, height = max(width, 250), max(height, 50)
    margin = max(18, round(60 * scale))
    x = margin if row.position == "bottom-left" else config.width - margin - width
    y = config.height - margin - height
    shadow_pad = max(4, round(10 * scale))
    canvas = Image.new("RGBA", (width + 2 * shadow_pad, height + 2 * shadow_pad))
    panel = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(panel)
    radius = max(6, round(16 * scale))
    bounds = (shadow_pad, shadow_pad, shadow_pad + width - 1, shadow_pad + height - 1)
    draw.rounded_rectangle(bounds, radius, fill=(13, 17, 23, 218), outline=(255, 255, 255, 32), width=1)
    # Multiply the rounded panel's existing alpha so the fade cannot create
    # pixels in the transparent shadow padding or rounded corner cutouts.
    alpha = panel.getchannel("A")
    fade = Image.new("L", canvas.size, 255)
    fade_draw = ImageDraw.Draw(fade)
    panel_left, panel_top, panel_right, panel_bottom = bounds
    fade_start = panel_left + round(width * .88)
    fade_span = max(1, panel_right - fade_start)
    for px in range(fade_start, panel_right + 1):
        factor = round(255 * (panel_right - px) / fade_span)
        fade_draw.line((px, panel_top, px, panel_bottom), fill=factor)
    panel.putalpha(ImageChops.multiply(alpha, fade))
    shadow = panel.getchannel("A").filter(ImageFilter.GaussianBlur(max(2, round(8 * scale))))
    canvas.paste((0, 0, 0, 110), (0, max(1, round(3 * scale))), shadow)
    canvas.alpha_composite(panel)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((shadow_pad, shadow_pad + radius // 2, shadow_pad + max(3, round(4 * scale)),
                            shadow_pad + height - radius // 2), 2, fill=(126, 231, 135, 255))
    avatar_size = max(24, round(64 * scale))
    av_x, av_y = shadow_pad + round(24 * scale), shadow_pad + (height - avatar_size) // 2
    canvas.alpha_composite(avatar.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS), (av_x, av_y))
    text_x = av_x + avatar_size + round(22 * scale)
    right_pad = round(24 * scale)
    text_width = shadow_pad + width - right_pad - text_x
    bold_path = config.font_bold_path or config.font_path
    regular_size = max(10, round(25 * scale))
    regular = ImageFont.truetype(str(config.font_path), regular_size)
    title_font, lines = _fit_title(
        draw, row.title, bold_path, text_width, max(22, round(70 * scale)),
        max(15, round(46 * scale)), max(8, round(24 * scale)),
    )
    title_line_h = draw.textbbox((0, 0), "Ag", font=title_font)[3]
    title_y = shadow_pad + round(21 * scale) if len(lines) == 1 else shadow_pad + round(12 * scale)
    for i, line in enumerate(lines):
        draw.text((text_x, title_y + i * round(title_line_h * 1.05)), line, font=title_font, fill=(255, 255, 255, 255))
    title_bottom = title_y + len(lines) * round(title_line_h * 1.05)
    detail_y = max(shadow_pad + round(94 * scale), title_bottom + round(5 * scale))
    username_box = draw.textbbox((text_x, detail_y), row.username, font=regular)
    draw.text((text_x, detail_y), row.username, font=regular, fill=(230, 237, 243, 235))
    octo_size = max(10, round(24 * scale))
    octo_x = shadow_pad + width - right_pad - octo_size
    if username_box[2] + round(14 * scale) > octo_x:
        raise MontageError(f"username and Octocat cannot fit for {row.username}")
    octo_y = detail_y
    canvas.alpha_composite(_load_octocat(config, octo_size), (octo_x, octo_y))
    output_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_png)
    return LowerThirdLayout(
        canvas.width, canvas.height, x - shadow_pad, y - shadow_pad, lines, title_font.size,
        (av_x, av_y, av_x + avatar_size, av_y + avatar_size),
        (text_x, title_y, text_x + text_width, title_bottom), username_box,
        (octo_x, octo_y, octo_x + octo_size, octo_y + octo_size),
    )


def overlay_expressions(layout: LowerThirdLayout, duration: Decimal, transition: Decimal) -> tuple[str, str]:
    enter_start = transition
    enter_end = enter_start + Decimal("0.25")
    exit_end = duration - transition
    exit_start = exit_end - Decimal("0.225")
    if exit_start <= enter_end:
        raise MontageError("clip is too short for lower-third animation")
    x = str(layout.x)
    # FFmpeg's overlay expression uses t; cubic easing stays still during hold.
    y = (
        f"if(lt(t,{decimal_text(enter_start)}),{layout.y + 16},"
        f"if(lt(t,{decimal_text(enter_end)}),"
        f"{layout.y}+16*(1-(t-{decimal_text(enter_start)})/0.25)^3,"
        f"if(lt(t,{decimal_text(exit_start)}),{layout.y},"
        f"if(lt(t,{decimal_text(exit_end)}),{layout.y}+12*((t-{decimal_text(exit_start)})/0.225)^3,"
        f"{layout.y + 12}))))"
    )
    return x, y


def build_segment_command(
    row: GameRow, media: MediaInfo, config: MontageConfig, overlay_path: Path,
    output_path: Path, ffmpeg: str = "ffmpeg", layout: LowerThirdLayout | None = None,
    timing: CycleTiming | None = None,
) -> list[str]:
    frame_count = timing.frame_counts[0] if timing and len(timing.frame_counts) == 1 else int(
        (media.duration * config.fps).to_integral_value(rounding=ROUND_HALF_UP)
    )
    duration = Decimal(frame_count) / config.fps
    transition = timing.transition if timing else Decimal(
        int((config.transition_duration * config.fps).to_integral_value(rounding=ROUND_HALF_UP))
    ) / config.fps
    if layout is None:
        layout = LowerThirdLayout(config.width, config.height, 0, 0, (), 0, (0, 0, 0, 0),
                                  (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0))
    x, y = overlay_expressions(layout, duration, transition)
    enter = transition
    exit_start = duration - transition - Decimal("0.225")
    video_filter = (
        f"[0:v]split=2[bg0][fg0];"
        f"[bg0]scale={config.width}:{config.height}:force_original_aspect_ratio=increase,"
        f"crop={config.width}:{config.height},gblur=sigma=18,eq=brightness=-0.18[bg];"
        f"[fg0]scale={config.width}:{config.height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[game];"
        f"[1:v]format=rgba,trim=duration={decimal_text(duration)},"
        f"fade=t=in:st={decimal_text(enter)}:d=0.25:alpha=1,"
        f"fade=t=out:st={decimal_text(exit_start)}:d=0.225:alpha=1[panel];"
        f"[game][panel]overlay=x='{x}':y='{y}':enable='between(t,{decimal_text(enter)},{decimal_text(duration-transition)})':"
        f"format=auto,fps={config.fps},trim=end_frame={frame_count},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v]"
    )
    audio_index = 0 if media.has_audio else 2
    loudness = "loudnorm=I=-16:TP=-1.5:LRA=11," if media.has_audio else ""
    sample_count = frame_count * (SAMPLE_RATE // config.fps)
    audio_filter = (
        f"[{audio_index}:a]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,{loudness}"
        "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"apad,atrim=end_sample={sample_count}[a]"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(row.clip_path),
               "-loop", "1", "-framerate", str(config.fps), "-i", str(overlay_path)]
    if not media.has_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    command.extend([
        "-filter_complex", f"{video_filter};{audio_filter}", "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", config.preset, "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(config.fps), "-c:a", "pcm_s16le", str(output_path),
    ])
    return command


def instagram_panel_height(width: int) -> int:
    height = int((Decimal(width) * 9 / 16).to_integral_value(rounding=ROUND_HALF_UP))
    return height + (height % 2)


def render_instagram_card(
    row: GameRow, avatar: Image.Image, config: MontageConfig, output_png: Path
) -> InstagramLayout:
    width, height = config.width, config.height
    panel_h = instagram_panel_height(width)
    margin = max(24, round(width * 0.06))
    canvas = Image.new("RGBA", (width, height), (13, 17, 23, 255))
    # Gameplay occupies the top panel at render time; keep it dark in the card.
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 0, width - 1, panel_h - 1), fill=(0, 0, 0, 255))
    background_bounds = (0, panel_h, width, height)
    try:
        with Image.open(config.instagram_background) as source:
            backdrop = source.convert("RGBA")
    except (OSError, UnidentifiedImageError) as error:
        raise MontageError(
            f"could not decode Instagram background {config.instagram_background}: {error}"
        ) from error
    region_w, region_h = width, height - panel_h
    scale = max(region_w / backdrop.width, region_h / backdrop.height)
    resized = backdrop.resize(
        (max(1, round(backdrop.width * scale)), max(1, round(backdrop.height * scale))),
        Image.Resampling.LANCZOS,
    )
    crop_left = (resized.width - region_w) // 2
    crop_top = (resized.height - region_h) // 2
    backdrop = resized.crop((crop_left, crop_top, crop_left + region_w, crop_top + region_h))
    canvas.alpha_composite(backdrop, (0, panel_h))
    # Darken the lower region slightly for text legibility over any artwork.
    scrim = Image.new("RGBA", (region_w, region_h), (13, 17, 23, 120))
    canvas.alpha_composite(scrim, (0, panel_h))
    draw = ImageDraw.Draw(canvas)

    bold_path = config.font_bold_path or config.font_path
    regular_path = config.font_path
    content_left = margin
    content_width = width - 2 * margin

    # Footer: wordmark bottom-left (Hubot Sans), white Invertocat bottom-right.
    footer_bottom = height - margin
    hubot_path = config.hubot_font_path or bold_path
    wordmark_text = "JS13KGAMES"
    foot_size = max(20, round(width * 0.044))
    hubot_font = ImageFont.truetype(str(hubot_path), foot_size)
    wm_box = draw.textbbox((0, 0), wordmark_text, font=hubot_font)
    cap_height = wm_box[3] - wm_box[1]
    wm_width = wm_box[2] - wm_box[0]
    wm_draw_x = content_left - wm_box[0]
    wm_draw_y = footer_bottom - wm_box[3]
    draw.text((wm_draw_x, wm_draw_y), wordmark_text, font=hubot_font, fill=(255, 255, 255, 255))
    wordmark_bounds = (content_left, footer_bottom - cap_height, content_left + wm_width, footer_bottom)
    logo_size = cap_height
    octo_x = width - margin - logo_size
    octo_y = footer_bottom - logo_size
    canvas.alpha_composite(_load_octocat(config, logo_size), (octo_x, octo_y))
    octocat_bounds = (octo_x, octo_y, octo_x + logo_size, octo_y + logo_size)
    footer_top = min(wordmark_bounds[1], octocat_bounds[1])

    # Metadata (Mona Sans): place, title, avatar, creator.
    content_top = panel_h + margin
    place_size = max(16, round(width * 0.040))
    place_font = ImageFont.truetype(str(bold_path), place_size)
    place_text = f"{row.place} place"
    place_box = draw.textbbox((content_left, content_top), place_text, font=place_font)
    draw.text((content_left, content_top), place_text, font=place_font, fill=(126, 231, 135, 255))
    place_bounds = (content_left, content_top, place_box[2], place_box[3])

    title_top = place_bounds[3] + max(10, round(height * 0.010))
    title_font, lines = _fit_title(
        draw, row.title, bold_path, content_width, round(height * 0.26),
        max(36, round(width * 0.085)), max(22, round(width * 0.050)),
    )
    title_line_h = draw.textbbox((0, 0), "Ag", font=title_font)[3]
    title_y = title_top
    for i, line in enumerate(lines):
        draw.text((content_left, title_y + i * round(title_line_h * 1.08)), line,
                  font=title_font, fill=(255, 255, 255, 255))
    title_bottom = title_y + len(lines) * round(title_line_h * 1.08)
    title_bounds = (content_left, title_top, content_left + content_width, title_bottom)

    avatar_size = max(48, round(width * 0.10))
    avatar_top = title_bottom + max(18, round(height * 0.020))
    avatar_top = min(avatar_top, footer_top - avatar_size - margin)
    av_x, av_y = content_left, avatar_top
    canvas.alpha_composite(avatar.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS), (av_x, av_y))
    avatar_bounds = (av_x, av_y, av_x + avatar_size, av_y + avatar_size)

    creator_size = max(16, round(width * 0.036))
    creator_font = ImageFont.truetype(str(regular_path), creator_size)
    creator_x = av_x + avatar_size + max(16, round(width * 0.022))
    creator_box = draw.textbbox((0, 0), row.username, font=creator_font)
    creator_h = creator_box[3] - creator_box[1]
    creator_y = av_y + (avatar_size - creator_h) // 2 - creator_box[1]
    draw.text((creator_x, creator_y), row.username, font=creator_font, fill=(230, 237, 243, 255))
    creator_bounds = (creator_x, av_y + (avatar_size - creator_h) // 2,
                      creator_x + (creator_box[2] - creator_box[0]), av_y + (avatar_size + creator_h) // 2)

    output_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output_png)
    return InstagramLayout(
        width, height, panel_h, background_bounds, place_bounds, title_bounds,
        avatar_bounds, creator_bounds, wordmark_bounds, octocat_bounds, lines, title_font.size,
    )


def build_instagram_segment_command(
    row: GameRow, media: MediaInfo, config: MontageConfig, card_path: Path,
    output_path: Path, ffmpeg: str = "ffmpeg", timing: CycleTiming | None = None,
) -> list[str]:
    frame_count = timing.frame_counts[0] if timing and len(timing.frame_counts) == 1 else int(
        (media.duration * config.fps).to_integral_value(rounding=ROUND_HALF_UP)
    )
    duration = Decimal(frame_count) / config.fps
    panel_h = instagram_panel_height(config.width)
    video_filter = (
        f"[0:v]split=2[bg0][fg0];"
        f"[bg0]scale={config.width}:{panel_h}:force_original_aspect_ratio=increase,"
        f"crop={config.width}:{panel_h},gblur=sigma=18,eq=brightness=-0.18[bg];"
        f"[fg0]scale={config.width}:{panel_h}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[panel];"
        f"[1:v]format=rgba,trim=duration={decimal_text(duration)}[card];"
        f"[card][panel]overlay=x=0:y=0:format=auto,"
        f"fps={config.fps},trim=end_frame={frame_count},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v]"
    )
    audio_index = 0 if media.has_audio else 2
    loudness = "loudnorm=I=-16:TP=-1.5:LRA=11," if media.has_audio else ""
    sample_count = frame_count * (SAMPLE_RATE // config.fps)
    audio_filter = (
        f"[{audio_index}:a]asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0,"
        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,{loudness}"
        "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"apad,atrim=end_sample={sample_count}[a]"
    )
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(row.clip_path),
               "-loop", "1", "-framerate", str(config.fps), "-i", str(card_path)]
    if not media.has_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    command.extend([
        "-filter_complex", f"{video_filter};{audio_filter}", "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", config.preset, "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(config.fps), "-c:a", "pcm_s16le", str(output_path),
    ])
    return command


def build_combine_command(
    segments: Sequence[Path], durations: Sequence[Decimal], config: MontageConfig,
    filter_path: Path, staging_path: Path, ffmpeg: str = "ffmpeg",
    timing: CycleTiming | None = None,
) -> list[str]:
    if timing is None:
        counts = [int((d * config.fps).to_integral_value(rounding=ROUND_HALF_UP)) for d in durations]
        timing = calculate_cycle_timing(counts, config.fps, config.transition_duration, config.loop_seam)
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-filter_complex_threads", "1", "-y"]
    cyclic = list(segments) + [segments[0]]
    for segment in cyclic:
        command.extend(["-i", str(segment)])
    graph: list[str] = []
    video_input, audio_input = "[0:v]", "[0:a]"
    cumulative = timing.frame_counts[0]
    for index in range(1, len(cyclic)):
        offset_frames = cumulative - index * timing.transition_frames
        offset = Decimal(offset_frames) / config.fps
        vlabel, alabel = f"vx{index}", f"ax{index}"
        graph.append(
            f"{video_input}[{index}:v]xfade=transition=fade:duration={decimal_text(timing.transition)}:"
            f"offset={decimal_text(offset)}[{vlabel}]"
        )
        graph.append(
            f"{audio_input}[{index}:a]acrossfade=nb_samples={timing.transition_frames * timing.samples_per_frame}:"
            f"c1=tri:c2=tri[{alabel}]"
        )
        video_input, audio_input = f"[{vlabel}]", f"[{alabel}]"
        if index < len(timing.frame_counts):
            cumulative += timing.frame_counts[index]
    end_frame = timing.seam_frame + timing.cycle_frames
    start_sample = timing.seam_frame * timing.samples_per_frame
    end_sample = start_sample + timing.cycle_samples
    graph.extend([
        f"{video_input}trim=start_frame={timing.seam_frame}:end_frame={end_frame},setpts=PTS-STARTPTS[vout]",
        f"{audio_input}atrim=start_sample={start_sample}:end_sample={end_sample},asetpts=PTS-STARTPTS[aout]",
    ])
    filter_path.write_text(";\n".join(graph), encoding="utf-8")
    command.extend([
        "-filter_complex_script", str(filter_path), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf), "-pix_fmt", "yuv420p",
        "-r", str(config.fps), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(staging_path),
    ])
    return command


def _case_insensitive_target() -> bool:
    """Whether the target platform's default filesystem aliases paths by case."""
    return sys.platform == "darwin" or os.name == "nt"


def paths_collide(first: Path, second: Path) -> bool:
    """Return True if two paths would resolve to the same destination.

    Prefers a true same-file check (os.path.samefile) when both paths exist so
    genuine aliases (hard/symbolic links, bind mounts) are caught regardless of
    platform. For paths that may not yet exist, compares normalized resolved
    paths and additionally folds case on platforms whose default filesystem is
    case-insensitive (Darwin, Windows), where a case-only difference still
    aliases the same file.
    """
    try:
        if os.path.samefile(first, second):
            return True
    except OSError:
        pass
    first_resolved = os.fspath(first.resolve())
    second_resolved = os.fspath(second.resolve())
    if first_resolved == second_resolved:
        return True
    if _case_insensitive_target():
        return first_resolved.casefold() == second_resolved.casefold()
    return False


def validate_inputs(config: MontageConfig, rows: Sequence[GameRow]) -> None:
    if config.width <= 0 or config.height <= 0 or config.width % 2 or config.height % 2:
        raise MontageError("output width and height must be positive even integers")
    if config.fps <= 0 or SAMPLE_RATE % config.fps:
        raise MontageError("fps must be positive and divide 48000")
    if not config.octocat_path.is_file():
        raise MontageError(f"Octocat image is missing or not a file: {config.octocat_path}")
    for label, path in (("font", config.font_path), ("bold font", config.font_bold_path or config.font_path)):
        if not path.is_file():
            raise MontageError(f"{label} is missing or not a file: {path}; supply a Mona Sans TTF/OTF")
    outputs = (config.output_path, config.preview_path, config.contact_sheet_path, config.loop_test_path)
    existing = [str(path) for path in outputs if path.exists()]
    if existing and not config.force:
        raise MontageError(f"output artifact(s) already exist (use --force): {', '.join(existing)}")
    if any(paths_collide(config.output_path, row.clip_path) for row in rows):
        raise MontageError("output path must not overwrite an input clip")
    if config.instagram_enabled:
        if config.instagram_width <= 0 or config.instagram_height <= 0 \
                or config.instagram_width % 2 or config.instagram_height % 2:
            raise MontageError("Instagram width and height must be positive even integers")
        background = config.instagram_background
        if background is None or not background.is_file():
            raise MontageError(f"Instagram background is missing or not a file: {background}")
        try:
            with Image.open(background) as image:
                image.verify()
        except (OSError, UnidentifiedImageError) as error:
            raise MontageError(f"Instagram background could not be decoded: {background} ({error})") from error
        hubot = config.hubot_font_path or config.font_bold_path or config.font_path
        if not hubot.is_file():
            raise MontageError(f"Hubot Sans font is missing or not a file: {hubot}; supply a Hubot Sans TTF/OTF")
        portrait_output = config.instagram_output_path
        if portrait_output.exists() and not config.force:
            raise MontageError(f"Instagram output already exists (use --force): {portrait_output}")
        artifact_paths = (
            ("the landscape output path", config.output_path),
            ("the lower-third preview", config.preview_path),
            ("the contact sheet", config.contact_sheet_path),
            ("the loop-test video", config.loop_test_path),
        )
        for label, artifact in artifact_paths:
            if paths_collide(portrait_output, artifact):
                raise MontageError(
                    f"Instagram output must differ from {label}: {portrait_output.resolve()}")
        if any(paths_collide(portrait_output, row.clip_path) for row in rows):
            raise MontageError("Instagram output must not overwrite an input clip")
    dependencies = ["ffmpeg", "ffprobe"] + (["rsvg-convert"] if config.octocat_path.suffix.lower() == ".svg" else [])
    for dependency in dependencies:
        if shutil.which(dependency) is None:
            raise MontageError(f"required dependency not found on PATH: {dependency}")
    checks = (
        (["ffmpeg", "-hide_banner", "-filters"], ("overlay", "xfade", "acrossfade"), "filters"),
        (["ffmpeg", "-hide_banner", "-encoders"], ("libx264", "aac"), "encoders"),
    )
    for command, required, description in checks:
        result = subprocess.run(command, text=True, capture_output=True)
        available = result.stdout + result.stderr
        missing = [name for name in required if name not in available]
        if result.returncode or missing:
            raise MontageError(f"FFmpeg is missing required {description}: {', '.join(missing) or 'unable to inspect'}")


def verify_render(path: Path, config: MontageConfig, stage: str) -> MediaInfo:
    info = probe_media(path)
    if not info.has_audio:
        raise MontageError(f"{stage}: rendered file has no audio stream: {path}")
    if (info.width, info.height) != (config.width, config.height):
        raise MontageError(f"{stage}: expected {config.width}x{config.height}, got {info.width}x{info.height}")
    if info.frame_rate is None or abs(float(info.frame_rate) - config.fps) > .01:
        raise MontageError(f"{stage}: expected {config.fps} fps, got {info.frame_rate}")
    return info


def _extract_frame(video: Path, time: Decimal, output: Path, ffmpeg: str = "ffmpeg") -> None:
    run_command([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", decimal_text(time),
                 "-i", str(video), "-frames:v", "1", str(output)], f"frame extraction from {video.name}")


def _create_contact_sheet(frames: Sequence[Path], output: Path) -> None:
    with Image.open(frames[0]) as first:
        cell_w, cell_h = first.size
    thumb_w, thumb_h = 480, round(480 * cell_h / cell_w)
    gap = 12
    sheet = Image.new("RGB", (4 * thumb_w + 5 * gap, 4 * thumb_h + 5 * gap), (13, 17, 23))
    for index, frame in enumerate(frames):
        with Image.open(frame) as image:
            thumb = image.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        x = gap + (index % 4) * (thumb_w + gap)
        y = gap + (index // 4) * (thumb_h + gap)
        sheet.paste(thumb, (x, y))
    sheet.save(output, quality=92, optimize=True)


def _build_loop_test(source: Path, output: Path, config: MontageConfig, duration: Decimal) -> None:
    span = min(Decimal("2"), duration / 4)
    start = duration - span
    graph = (
        f"[0:v]trim=start={decimal_text(start)}:end={decimal_text(duration)},setpts=PTS-STARTPTS[v0];"
        f"[0:a]atrim=start={decimal_text(start)}:end={decimal_text(duration)},asetpts=PTS-STARTPTS[a0];"
        f"[0:v]trim=start=0:end={decimal_text(span)},setpts=PTS-STARTPTS[v1];"
        f"[0:a]atrim=start=0:end={decimal_text(span)},asetpts=PTS-STARTPTS[a1];"
        "[v0][v1]concat=n=2:v=1:a=0[v];[a0][a1]concat=n=2:v=0:a=1[a]"
    )
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y", "-i", str(source),
        "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
        "-preset", config.preset, "-crf", str(config.crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output),
    ], "loop-test render", config.verbose)


def _default_font(name: str) -> Path | None:
    candidates = [
        Path("assets") / name,
        Path("/Library/Fonts") / name,
        Path.home() / "Library/Fonts" / name,
    ]
    return next((path for path in candidates if path.is_file()), None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a normalized, seamlessly cyclic gameplay montage in CSV order.")
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--octocat", type=Path, default=os.environ.get("OCTOCAT_PATH"))
    parser.add_argument("--font", type=Path, default=os.environ.get("FONT_PATH"))
    parser.add_argument("--font-bold", type=Path, default=os.environ.get("FONT_BOLD_PATH"))
    parser.add_argument("--avatar-cache", type=Path)
    parser.add_argument("--avatar-timeout", type=float, default=8)
    parser.add_argument("--refresh-avatars", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--loop-seam", type=Decimal, default=Decimal("1.5"))
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--transition-duration", type=Decimal, default=Decimal("0.65"))
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--instagram-background", type=Path,
                        default=os.environ.get("INSTAGRAM_BACKGROUND"))
    parser.add_argument("--instagram-output", type=Path,
                        default=os.environ.get("INSTAGRAM_OUTPUT"))
    parser.add_argument("--hubot-font", type=Path, default=os.environ.get("HUBOT_FONT_PATH"))
    parser.add_argument("--instagram-width", type=int, default=1080)
    parser.add_argument("--instagram-height", type=int, default=1920)
    return parser


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> MontageConfig:
    octocat = args.octocat or Path("assets/GitHub_Invertocat_White.svg")
    font = args.font or _default_font("MonaSans-Regular.ttf")
    bold = args.font_bold or _default_font("MonaSans-Bold.ttf")
    if font is None or bold is None:
        parser.error("Mona Sans is required; use --font and --font-bold (or FONT_PATH/FONT_BOLD_PATH)")
    if not args.input_dir.is_dir():
        parser.error(f"input directory does not exist: {args.input_dir}")
    if not args.csv_file.is_file():
        parser.error(f"CSV file does not exist: {args.csv_file}")
    if args.transition_duration <= 0 or args.loop_seam <= 0:
        parser.error("--transition-duration and --loop-seam must be positive")
    if args.avatar_timeout <= 0:
        parser.error("--avatar-timeout must be positive")
    hubot = args.hubot_font or _default_font("HubotSans-Bold.ttf")
    if args.instagram_background is not None and hubot is None:
        parser.error(
            "Hubot Sans is required for the Instagram variant; use --hubot-font (or HUBOT_FONT_PATH)"
        )
    return MontageConfig(
        args.input_dir, args.csv_file, args.output_file, octocat, font, args.width, args.height,
        args.fps, args.transition_duration, args.keep_temp, args.force, args.crf, args.preset,
        args.verbose, args.avatar_cache, args.avatar_timeout, args.refresh_avatars, args.offline,
        args.loop_seam, bold,
        instagram_background=args.instagram_background, instagram_output=args.instagram_output,
        hubot_font_path=hubot, instagram_width=args.instagram_width,
        instagram_height=args.instagram_height,
    )


def create_montage(config: MontageConfig) -> None:
    rows = parse_csv(config.csv_path, config.input_dir)
    media = [probe_media(row.clip_path) for row in rows]
    validate_inputs(config, rows)
    avatar_results = [(row.github_login, fetch_avatar(row.github_login, config)) for row in rows]
    available = [login for login, path in avatar_results if path is not None]
    unavailable = [login for login, path in avatar_results if path is None]
    print(
        f"Avatars: verified {len(available)}/{len(rows)} real decoded cached images",
        file=sys.stderr,
    )
    if unavailable:
        print(
            "Avatars unavailable (initials fallback): "
            + ", ".join(f"@{login}" for login in unavailable),
            file=sys.stderr,
        )
    frame_counts = [
        int((item.duration * config.fps).to_integral_value(rounding=ROUND_HALF_UP)) for item in media
    ]
    timing = calculate_cycle_timing(frame_counts, config.fps, config.transition_duration, config.loop_seam)
    if timing.transition != config.transition_duration:
        print(f"Transition quantized to {timing.transition_frames} frames ({timing.transition}s)", file=sys.stderr)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix=".montage-work-", dir=str(config.output_path.parent)))
    staging = {
        config.output_path: work_dir / "output.mp4",
        config.preview_path: work_dir / "lower-third-preview.png",
        config.contact_sheet_path: work_dir / "montage-contact-sheet.jpg",
        config.loop_test_path: work_dir / "loop-test.mp4",
    }
    portrait_config = replace(config, width=config.instagram_width, height=config.instagram_height,
                              output_path=config.instagram_output_path) if config.instagram_enabled else None
    if portrait_config is not None:
        staging[portrait_config.output_path] = work_dir / "output-instagram.mp4"
    try:
        segments: list[Path] = []
        portrait_segments: list[Path] = []
        contact_frames: list[Path] = []
        for index, (row, info, frames) in enumerate(zip(rows, media, frame_counts), start=1):
            overlay = work_dir / f"segment-{index:03d}-lower-third.png"
            segment = work_dir / f"segment-{index:03d}.mkv"
            avatar = load_avatar_or_fallback(row, config, max(24, round(64 * config.height / 1080)))
            layout = render_lower_third(row, avatar, config, overlay)
            one_timing = CycleTiming((frames,), timing.transition_frames, timing.transition,
                                     timing.seam_frame, frames - timing.transition_frames,
                                     timing.samples_per_frame)
            run_command(build_segment_command(row, info, config, overlay, segment, layout=layout, timing=one_timing),
                        f"segment render for {row.clip_path}", config.verbose)
            verify_render(segment, config, "segment validation")
            segments.append(segment)
            if portrait_config is not None:
                card = work_dir / f"segment-{index:03d}-instagram.png"
                portrait_segment = work_dir / f"segment-{index:03d}-instagram.mkv"
                portrait_avatar = load_avatar_or_fallback(
                    row, config, max(48, round(portrait_config.width * 0.10)))
                render_instagram_card(row, portrait_avatar, portrait_config, card)
                run_command(
                    build_instagram_segment_command(row, info, portrait_config, card,
                                                    portrait_segment, timing=one_timing),
                    f"Instagram segment render for {row.clip_path}", config.verbose)
                verify_render(portrait_segment, portrait_config, "Instagram segment validation")
                portrait_segments.append(portrait_segment)
            frame = work_dir / f"contact-{index:03d}.png"
            stable_frame = Decimal(max(timing.seam_frame, timing.transition_frames + round(.4 * config.fps))) / config.fps
            _extract_frame(segment, stable_frame, frame)
            contact_frames.append(frame)
        filter_path = work_dir / "cyclic-filter.txt"
        run_command(build_combine_command(segments, [Decimal(n) / config.fps for n in frame_counts],
                                          config, filter_path, staging[config.output_path], timing=timing),
                    "cyclic montage combine", config.verbose)
        final_info = verify_render(staging[config.output_path], config, "final validation")
        if portrait_config is not None:
            portrait_filter = work_dir / "cyclic-filter-instagram.txt"
            run_command(build_combine_command(portrait_segments, [Decimal(n) / config.fps for n in frame_counts],
                                              portrait_config, portrait_filter,
                                              staging[portrait_config.output_path], timing=timing),
                        "cyclic Instagram combine", config.verbose)
            verify_render(staging[portrait_config.output_path], portrait_config, "Instagram final validation")
        shutil.copyfile(contact_frames[0], staging[config.preview_path])
        _create_contact_sheet(contact_frames, staging[config.contact_sheet_path])
        _build_loop_test(staging[config.output_path], staging[config.loop_test_path], config, final_info.duration)
        verify_render(staging[config.loop_test_path], config, "loop-test validation")
        with Image.open(staging[config.preview_path]) as image:
            image.verify()
        with Image.open(staging[config.contact_sheet_path]) as image:
            image.verify()
        for destination, source in staging.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
    finally:
        if config.keep_temp:
            print(f"Kept montage work directory: {work_dir}", file=sys.stderr)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        config = config_from_args(parser.parse_args(argv), parser)
        create_montage(config)
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    except (MontageError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Created montage and supporting artifacts beside: {config.output_path}")
    if config.instagram_enabled:
        print(f"Created Instagram 9:16 montage: {config.instagram_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
