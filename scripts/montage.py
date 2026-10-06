#!/usr/bin/env python3
"""Reproducible FFmpeg gameplay montage builder."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html import escape
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Sequence


PLACE_HEADER = "Place"
TITLE_HEADER = "Game title"
USER_HEADER = "GitHub usernames with @ that created it"
REQUIRED_HEADERS = (PLACE_HEADER, TITLE_HEADER, USER_HEADER)


@dataclass(frozen=True)
class GameRow:
    row_number: int
    place: str
    place_number: int
    title: str
    username: str
    clip_path: Path


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: Decimal
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    frame_rate: Fraction | None


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


class MontageError(RuntimeError):
    pass


def parse_place_number(value: str) -> int:
    match = re.match(r"^\s*([1-9][0-9]*)", value or "")
    if not match:
        raise ValueError(f"placement must start with a positive integer: {value!r}")
    return int(match.group(1))


def normalize_username(value: str) -> str:
    username = (value or "").strip().lstrip("@")
    if not username:
        raise ValueError("username is blank")
    return f"@{username}"


def parse_csv(path: Path, input_dir: Path) -> list[GameRow]:
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as error:
        raise ValueError(f"cannot read CSV {path}: {error}") from error
    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV {path} is empty")
        normalized_headers = {(name or "").strip(): name for name in reader.fieldnames}
        missing = [name for name in REQUIRED_HEADERS if name not in normalized_headers]
        if missing:
            raise ValueError(f"CSV missing required header(s): {', '.join(missing)}")
        keys = {name: normalized_headers[name] for name in REQUIRED_HEADERS}
        rows: list[GameRow] = []
        placements: set[int] = set()
        for logical_row, raw in enumerate(reader, start=2):
            values = [
                (raw.get(keys[PLACE_HEADER]) or "").strip(),
                (raw.get(keys[TITLE_HEADER]) or "").strip(),
                (raw.get(keys[USER_HEADER]) or "").strip(),
            ]
            if not any(values):
                continue
            if not all(values):
                raise ValueError(f"CSV row {logical_row} has a blank required field")
            try:
                place_number = parse_place_number(values[0])
                username = normalize_username(values[2])
            except ValueError as error:
                raise ValueError(f"CSV row {logical_row}: {error}") from error
            if place_number in placements:
                raise ValueError(f"CSV row {logical_row}: duplicate placement {place_number}")
            clip = input_dir / f"{place_number}.mp4"
            if not clip.is_file():
                raise ValueError(f"CSV row {logical_row}: missing clip {clip}")
            placements.add(place_number)
            rows.append(
                GameRow(logical_row, values[0], place_number, values[1], username, clip)
            )
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
    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
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
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        raise MontageError(f"no video stream in {path}")
    audio = any(stream.get("codec_type") == "audio" for stream in streams)
    duration_value = video.get("duration") or data.get("format", {}).get("duration")
    duration = _positive_decimal(duration_value, f"duration for {path}")
    rate_value = video.get("avg_frame_rate") or video.get("r_frame_rate")
    try:
        rate = Fraction(rate_value) if rate_value and rate_value != "0/0" else None
    except (ValueError, ZeroDivisionError):
        rate = None
    return MediaInfo(
        path=path,
        duration=duration,
        has_video=True,
        has_audio=audio,
        width=video.get("width"),
        height=video.get("height"),
        frame_rate=rate,
    )


def calculate_transition_offsets(
    durations: Sequence[Decimal], transition: Decimal
) -> list[Decimal]:
    if transition <= 0:
        raise ValueError("transition duration must be positive")
    for left, right in zip(durations, durations[1:]):
        if transition >= left or transition >= right:
            raise ValueError("transition duration must be shorter than every adjacent clip")
    return [
        sum(durations[:index], Decimal(0)) - transition * index
        for index in range(1, len(durations))
    ]


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def run_command(argv: Sequence[str], stage: str, verbose: bool = False) -> None:
    if verbose:
        print(f"[{stage}] {' '.join(argv)}", file=sys.stderr)
    try:
        result = subprocess.run(list(argv), text=True, capture_output=not verbose)
    except FileNotFoundError as error:
        raise MontageError(f"{stage}: dependency not found: {argv[0]}") from error
    if result.returncode:
        stderr = "" if verbose else (result.stderr or "")
        tail = "\n".join(stderr.strip().splitlines()[-20:])
        raise MontageError(
            f"{stage} failed with exit code {result.returncode}"
            + (f":\n{tail}" if tail else "")
        )


def write_lower_third_svg(row: GameRow, config: MontageConfig, output: Path) -> None:
    scale = config.height / 1080
    margin = max(18, round(64 * scale))
    box_width = min(config.width - 2 * margin, max(round(820 * scale), config.width // 2))
    box_height = max(88, round(150 * scale))
    box_y = config.height - margin - box_height
    radius = max(10, round(18 * scale))
    logo_size = max(40, round(78 * scale))
    logo_x = margin + max(10, round(22 * scale))
    logo_y = box_y + (box_height - logo_size) // 2
    text_x = logo_x + logo_size + max(12, round(24 * scale))
    title_y = box_y + max(36, round(56 * scale))
    detail_y = box_y + max(70, round(105 * scale))
    title_size = max(20, round(42 * scale))
    detail_size = max(14, round(26 * scale))
    detail = f"{row.username}  •  {row.place} Place"
    def embedded_uri(path: Path, fallback_type: str) -> str:
        media_type = mimetypes.guess_type(path.name)[0] or fallback_type
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{media_type};base64,{encoded}"

    # Embedding avoids librsvg's deliberate same-directory restriction on
    # external resources and guarantees the configured font is really used.
    font_uri = embedded_uri(config.font_path, "application/octet-stream")
    logo_uri = embedded_uri(config.octocat_path, "application/octet-stream")
    title = escape(row.title)
    detail_text = escape(detail)
    output.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="{config.width}" height="{config.height}">
<style>@font-face{{font-family:MontageFont;src:url('{font_uri}')}}</style>
<rect x="{margin}" y="{box_y}" width="{box_width}" height="{box_height}" rx="{radius}" fill="#090b10" fill-opacity=".78"/>
<image href="{logo_uri}" x="{logo_x}" y="{logo_y}" width="{logo_size}" height="{logo_size}" preserveAspectRatio="xMidYMid meet"/>
<text x="{text_x}" y="{title_y}" font-family="MontageFont" font-size="{title_size}" font-weight="bold" fill="white">{title}</text>
<text x="{text_x}" y="{detail_y}" font-family="MontageFont" font-size="{detail_size}" fill="#e6edf3">{detail_text}</text>
</svg>
""",
        encoding="utf-8",
    )


def build_lower_third_command(
    svg_path: Path, output: Path, renderer: str = "rsvg-convert"
) -> list[str]:
    return [renderer, str(svg_path), "--output", str(output)]


def build_segment_command(
    row: GameRow,
    media: MediaInfo,
    config: MontageConfig,
    overlay_path: Path,
    output_path: Path,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    width, height = config.width, config.height
    video_filter = (
        f"[0:v]split=2[bg0][fg0];"
        f"[bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=18,eq=brightness=-0.18[bg];"
        f"[fg0]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[game];"
        f"[game][1:v]overlay=0:0:format=auto,fps={config.fps},setsar=1,"
        "format=yuv420p,setpts=PTS-STARTPTS[v]"
    )
    audio_index = 0 if media.has_audio else 2
    loudness = "loudnorm=I=-16:TP=-1.5:LRA=11," if media.has_audio else ""
    audio_filter = (
        f"[{audio_index}:a]asetpts=PTS-STARTPTS,"
        "aresample=48000:async=1:first_pts=0,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"{loudness}"
        f"apad,atrim=duration={decimal_text(media.duration)}[a]"
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(row.clip_path),
        "-loop",
        "1",
        "-i",
        str(overlay_path),
    ]
    if not media.has_audio:
        command.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
    command.extend(
        [
            "-filter_complex",
            f"{video_filter};{audio_filter}",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-t",
            decimal_text(media.duration),
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(config.fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    )
    return command


def build_combine_command(
    segments: Sequence[Path],
    durations: Sequence[Decimal],
    config: MontageConfig,
    filter_path: Path,
    staging_path: Path,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    # Keep complex-filter output deterministic across FFmpeg thread schedulers.
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-filter_complex_threads",
        "1",
        "-y",
    ]
    for segment in segments:
        command.extend(["-i", str(segment)])
    graph: list[str] = []
    if len(segments) == 1:
        video_map, audio_map = "0:v:0", "0:a:0"
    else:
        offsets = calculate_transition_offsets(durations, config.transition_duration)
        video_input = "[0:v]"
        for index, offset in enumerate(offsets, start=1):
            video_label = "vout" if index == len(segments) - 1 else f"v{index}"
            graph.append(
                f"{video_input}[{index}:v]xfade=transition=fade"
                f":duration={decimal_text(config.transition_duration)}"
                f":offset={decimal_text(offset)}[{video_label}]"
            )
            video_input = f"[{video_label}]"
        audio_input = "[0:a]"
        for index in range(1, len(segments)):
            audio_label = f"a{index}"
            graph.append(
                f"{audio_input}[{index}:a]acrossfade=d={decimal_text(config.transition_duration)}"
                f":c1=tri:c2=tri[{audio_label}]"
            )
            audio_input = f"[{audio_label}]"
        video_map, audio_map = "[vout]", audio_input
        filter_path.write_text(";\n".join(graph), encoding="utf-8")
        command.extend(["-filter_complex_script", str(filter_path)])
    command.extend(
        [
            "-map",
            video_map,
            "-map",
            audio_map,
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-crf",
            str(config.crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(config.fps),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(staging_path),
        ]
    )
    return command


def validate_inputs(config: MontageConfig, rows: Sequence[GameRow]) -> None:
    if config.width <= 0 or config.height <= 0 or config.width % 2 or config.height % 2:
        raise MontageError("output width and height must be positive even integers")
    if config.fps <= 0:
        raise MontageError("fps must be positive")
    if not config.octocat_path.is_file():
        raise MontageError(f"Octocat image is missing or not a file: {config.octocat_path}")
    if not config.font_path.is_file():
        raise MontageError(f"font is missing or not a file: {config.font_path}")
    if config.output_path.exists() and not config.force:
        raise MontageError(f"output already exists (use --force): {config.output_path}")
    output = config.output_path.resolve()
    if any(output == row.clip_path.resolve() for row in rows):
        raise MontageError("output path must not overwrite an input clip")
    for dependency in ("ffmpeg", "ffprobe", "rsvg-convert"):
        if shutil.which(dependency) is None:
            raise MontageError(f"required dependency not found on PATH: {dependency}")
    capability_checks = (
        (["ffmpeg", "-hide_banner", "-filters"], ("overlay", "xfade", "acrossfade"), "filters"),
        (["ffmpeg", "-hide_banner", "-encoders"], ("libx264", "aac"), "encoders"),
    )
    for command, required, description in capability_checks:
        result = subprocess.run(command, text=True, capture_output=True)
        available = result.stdout + result.stderr
        missing = [name for name in required if name not in available]
        if result.returncode or missing:
            raise MontageError(
                f"FFmpeg is missing required {description}: {', '.join(missing) or 'unable to inspect'}"
            )
    calculate_transition_offsets(
        [probe_media(row.clip_path).duration for row in rows], config.transition_duration
    )


def verify_render(path: Path, config: MontageConfig, stage: str) -> MediaInfo:
    info = probe_media(path)
    if not info.has_audio:
        raise MontageError(f"{stage}: rendered file has no audio stream: {path}")
    if (info.width, info.height) != (config.width, config.height):
        raise MontageError(
            f"{stage}: expected {config.width}x{config.height}, got {info.width}x{info.height}"
        )
    if info.frame_rate is None or abs(float(info.frame_rate) - config.fps) > 0.01:
        raise MontageError(f"{stage}: expected {config.fps} fps, got {info.frame_rate}")
    return info


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a normalized gameplay montage in CSV row order."
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("csv_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--octocat", type=Path, default=os.environ.get("OCTOCAT_PATH"))
    parser.add_argument("--font", type=Path, default=os.environ.get("FONT_PATH"))
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--transition-duration", type=Decimal, default=Decimal("0.75"))
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> MontageConfig:
    if args.octocat is None or args.font is None:
        parser.error(
            "--octocat and --font are required (or set OCTOCAT_PATH and FONT_PATH); "
            "example: --octocat assets/logo.png --font /path/to/font.ttf"
        )
    if not args.input_dir.is_dir():
        parser.error(f"input directory does not exist: {args.input_dir}")
    if not args.csv_file.is_file():
        parser.error(f"CSV file does not exist: {args.csv_file}")
    if args.transition_duration <= 0:
        parser.error("--transition-duration must be positive")
    return MontageConfig(
        args.input_dir,
        args.csv_file,
        args.output_file,
        args.octocat,
        args.font,
        args.width,
        args.height,
        args.fps,
        args.transition_duration,
        args.keep_temp,
        args.force,
        args.crf,
        args.preset,
        args.verbose,
    )


def create_montage(config: MontageConfig) -> None:
    rows = parse_csv(config.csv_path, config.input_dir)
    media = [probe_media(row.clip_path) for row in rows]
    validate_inputs(config, rows)
    calculate_transition_offsets([item.duration for item in media], config.transition_duration)
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(
        tempfile.mkdtemp(prefix=".montage-work-", dir=str(config.output_path.parent))
    )
    staging = config.output_path.parent / f".{config.output_path.name}.partial.mp4"
    try:
        segments: list[Path] = []
        for index, (row, info) in enumerate(zip(rows, media), start=1):
            overlay_svg = work_dir / f"segment-{index:03d}-lower-third.svg"
            overlay = work_dir / f"segment-{index:03d}-lower-third.png"
            segment = work_dir / f"segment-{index:03d}.mp4"
            write_lower_third_svg(row, config, overlay_svg)
            run_command(
                build_lower_third_command(overlay_svg, overlay),
                f"lower-third render for CSV row {row.row_number}",
                config.verbose,
            )
            run_command(
                build_segment_command(row, info, config, overlay, segment),
                f"segment render for {row.clip_path}",
                config.verbose,
            )
            verify_render(segment, config, "segment validation")
            segments.append(segment)
        filter_path = work_dir / "combine-filter.txt"
        run_command(
            build_combine_command(
                segments,
                [item.duration for item in media],
                config,
                filter_path,
                staging,
            ),
            "montage combine",
            config.verbose,
        )
        verify_render(staging, config, "final validation")
        os.replace(staging, config.output_path)
    finally:
        if staging.exists():
            staging.unlink()
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
    print(f"Created montage: {config.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
