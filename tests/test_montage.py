import csv
import io
import json
import os
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from scripts.montage import (
    MediaInfo,
    MontageError,
    MontageConfig,
    avatar_cache_path,
    build_combine_command,
    build_instagram_segment_command,
    calculate_cycle_timing,
    calculate_transition_offsets,
    fallback_initials,
    fetch_avatar,
    instagram_panel_height,
    normalize_username,
    parse_csv,
    parse_place_number,
    paths_collide,
    probe_media,
    render_instagram_card,
    render_lower_third,
    load_avatar_or_fallback,
    validate_inputs,
)


HEADERS = ["Place", "Game title", "GitHub usernames with @ that created it"]


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write_csv(self, rows, *, bom=False):
        path = self.root / "games.csv"
        encoding = "utf-8-sig" if bom else "utf-8"
        with path.open("w", encoding=encoding, newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADERS)
            writer.writerows(rows)
        return path

    def test_preserves_csv_order_and_matches_place(self):
        for number in (1, 2):
            (self.clips / f"{number}.mp4").touch()
        path = self.write_csv(
            [["2nd", 'A "quoted", title', "maker"], ["1st", "Ünicode", "@@user"]]
        )
        rows = parse_csv(path, self.clips)
        self.assertEqual([2, 1], [row.place_number for row in rows])
        self.assertEqual(["@maker", "@user"], [row.username for row in rows])
        self.assertEqual("A \"quoted\", title", rows[0].title)

    def test_accepts_bom_and_ignores_blank_rows(self):
        (self.clips / "13.mp4").touch()
        path = self.write_csv([[], [" 13th ", " Game ", " user "]], bom=True)
        rows = parse_csv(path, self.clips)
        self.assertEqual((13, "Game", "@user"), (rows[0].place_number, rows[0].title, rows[0].username))

    def test_rejects_duplicate_and_partial_rows(self):
        for number in (1, 2):
            (self.clips / f"{number}.mp4").touch()
        with self.assertRaisesRegex(ValueError, "duplicate placement"):
            parse_csv(self.write_csv([["1st", "A", "a"], ["1st", "B", "b"]]), self.clips)
        with self.assertRaisesRegex(ValueError, "CSV row 2"):
            parse_csv(self.write_csv([["2nd", "", "b"]]), self.clips)

    def test_rejects_missing_headers_clip_and_bad_place(self):
        bad = self.root / "bad.csv"
        bad.write_text("Place,Game title\n1st,A\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing required header"):
            parse_csv(bad, self.clips)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            parse_place_number("winner")
        with self.assertRaisesRegex(ValueError, "missing clip"):
            parse_csv(self.write_csv([["1st", "A", "a"]]), self.clips)

    def test_username_normalization(self):
        self.assertEqual("@name", normalize_username("name"))
        self.assertEqual("@name", normalize_username("@@name"))
        with self.assertRaisesRegex(ValueError, "invalid GitHub username"):
            normalize_username("../name")

    def test_position_override(self):
        for number in (1, 2):
            (self.clips / f"{number}.mp4").touch()
        path = self.root / "positions.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADERS + ["Lower third position"])
            writer.writerows([["1st", "Left", "one", ""], ["2nd", "Right", "two", "bottom-right"]])
        rows = parse_csv(path, self.clips)
        self.assertEqual(["bottom-left", "bottom-right"], [row.position for row in rows])
        self.assertEqual(["one", "two"], [row.github_login for row in rows])
        path.write_text(
            ",".join(HEADERS + ["Lower third position"]) + "\n1st,A,one,center\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "bottom-left or bottom-right"):
            parse_csv(path, self.clips)


class ProbeAndTimingTests(unittest.TestCase):
    @patch("scripts.montage.subprocess.run")
    def test_probe_finds_streams_by_type_and_fractional_fps(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps(
            {
                "streams": [
                    {"codec_type": "audio"},
                    {
                        "codec_type": "video",
                        "width": 1080,
                        "height": 1920,
                        "avg_frame_rate": "30000/1001",
                        "duration": "2.5",
                    },
                ],
                "format": {"duration": "2.6"},
            }
        )
        run.return_value.stderr = ""
        info = probe_media(Path("portrait.mp4"))
        self.assertEqual((1080, 1920), (info.width, info.height))
        self.assertTrue(info.has_audio)
        self.assertEqual(Decimal("2.5"), info.duration)
        self.assertEqual("30000/1001", str(info.frame_rate))

    def test_offsets_use_decimal_math(self):
        self.assertEqual(
            [Decimal("12.25"), Decimal("24.50")],
            calculate_transition_offsets([Decimal("13")] * 3, Decimal("0.75")),
        )
        self.assertEqual([], calculate_transition_offsets([Decimal("1")], Decimal("0.2")))
        with self.assertRaisesRegex(ValueError, "shorter"):
            calculate_transition_offsets([Decimal("1"), Decimal("0.5")], Decimal("0.5"))

    def test_cyclic_graph_repeats_first_and_trims_exact_cycle(self):
        with tempfile.TemporaryDirectory(dir=".") as temp:
            root = Path(temp)
            filter_path = root / "filter.txt"
            config = MontageConfig(
                root,
                root / "data.csv",
                root / "out.mp4",
                root / "logo.svg",
                root / "font.ttf",
                640,
                360,
                30,
                Decimal("0.75"),
                False,
                False,
            )
            build_combine_command(
                [root / f"{index}.mp4" for index in range(3)],
                [Decimal("3.25"), Decimal("5.5"), Decimal("2.125")],
                config,
                filter_path,
                root / "staging.mp4",
            )
            graph = filter_path.read_text(encoding="utf-8")

        self.assertEqual(3, graph.count("xfade=transition=fade"))
        self.assertEqual(3, graph.count("acrossfade="))
        self.assertIn("[vx2][3:v]xfade=", graph)
        self.assertIn("trim=start_frame=45:end_frame=303", graph)
        self.assertIn("atrim=start_sample=72000:end_sample=484800", graph)
        self.assertNotIn("concat=", graph)

    def test_frame_domain_cycle_math(self):
        timing = calculate_cycle_timing([390] * 13, 30, Decimal("0.65"), Decimal("1.5"))
        self.assertEqual(20, timing.transition_frames)
        self.assertEqual(45, timing.seam_frame)
        self.assertEqual(4810, timing.cycle_frames)
        self.assertEqual(1600, timing.samples_per_frame)
        self.assertEqual(7_696_000, timing.cycle_samples)
        unequal = calculate_cycle_timing([100, 120], 30, Decimal(".2"), Decimal("0.5"))
        self.assertEqual(208, unequal.cycle_frames)
        with self.assertRaisesRegex(ValueError, "loop seam"):
            calculate_cycle_timing([100, 100], 30, Decimal(".2"), Decimal(".1"))
        with self.assertRaisesRegex(ValueError, "divide 48000"):
            calculate_cycle_timing([100], 29, Decimal(".2"), Decimal("1"))


class AvatarAndGraphicsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name)
        font = Path(os.environ.get("MONTAGE_TEST_FONT", "/Library/Fonts/MonaSans-Regular.ttf"))
        bold = Path(os.environ.get("MONTAGE_TEST_FONT_BOLD", "/Library/Fonts/MonaSans-Bold.ttf"))
        if not font.is_file() or not bold.is_file():
            self.skipTest("Mona Sans test fonts unavailable")
        logo = self.root / "logo.png"
        Image.new("RGBA", (32, 32), "white").save(logo)
        self.config = MontageConfig(
            self.root, self.root / "data.csv", self.root / "out.mp4", logo, font,
            1920, 1080, 30, Decimal(".65"), False, False,
            avatar_cache=self.root / "cache", offline=True, font_bold_path=bold,
        )

    def tearDown(self):
        self.temp.cleanup()

    def row(self, *, title="A Beautiful Game", position="bottom-left"):
        return __import__("scripts.montage", fromlist=["GameRow"]).GameRow(
            2, "1st", 1, title, "@Octo-Cat", self.root / "1.mp4", "Octo-Cat", position
        )

    def test_cache_hit_and_deterministic_fallback(self):
        target = avatar_cache_path("@Octo-Cat", self.config.cache_path)
        target.parent.mkdir()
        Image.new("RGB", (100, 80), "purple").save(target)
        with patch("scripts.montage.urllib.request.urlopen") as request:
            self.assertEqual(target, fetch_avatar("Octo-Cat", self.config))
            request.assert_not_called()
        self.assertEqual("OC", fallback_initials("Octo-Cat"))
        target.unlink()
        avatar = load_avatar_or_fallback(self.row(), self.config, 64)
        self.assertEqual((64, 64), avatar.size)
        self.assertEqual(0, avatar.getpixel((0, 0))[3])

    def image_response(self, color="green"):
        buffer = io.BytesIO()
        Image.new("RGB", (40, 40), color).save(buffer, format="PNG")
        response = MagicMock()
        response.headers.get_content_type.return_value = "image/png"
        response.read.return_value = buffer.getvalue()
        response.__enter__.return_value = response
        return response

    def test_download_populates_verified_cache_and_renders_real_avatar(self):
        response = self.image_response()
        with patch("scripts.montage.urllib.request.urlopen", return_value=response) as opener:
            online = __import__("dataclasses").replace(self.config, offline=False)
            path = fetch_avatar("Octo-Cat", online)
        self.assertTrue(path and path.is_file())
        with Image.open(path) as cached:
            cached.verify()
        avatar = load_avatar_or_fallback(self.row(), online, 64)
        self.assertEqual((0, 128, 0, 255), avatar.getpixel((32, 32)))
        request = opener.call_args.args[0]
        self.assertEqual("https://github.com/Octo-Cat.png?size=256", request.full_url)
        self.assertIn("image/png", request.get_header("Accept"))
        self.assertTrue(request.get_header("User-agent").startswith("js13k-montage/"))
        self.assertEqual(8.0, opener.call_args.kwargs["timeout"])
        self.assertFalse(list(path.parent.glob("*.partial")))

    def test_corrupt_cache_is_recovered_online_atomically(self):
        target = avatar_cache_path("Octo-Cat", self.config.cache_path)
        target.parent.mkdir()
        target.write_bytes(b"not an image")
        online = __import__("dataclasses").replace(self.config, offline=False)
        with patch("scripts.montage.urllib.request.urlopen", return_value=self.image_response("red")):
            with self.assertWarnsRegex(UserWarning, "downloading a replacement"):
                path = fetch_avatar("@@Octo-Cat", online)
        self.assertEqual(target, path)
        with Image.open(target) as cached:
            self.assertEqual((255, 0, 0), cached.convert("RGB").getpixel((20, 20)))
        self.assertFalse(list(target.parent.glob("*.partial")))

    def test_avatar_download_failures_warn_and_fallback(self):
        online = __import__("dataclasses").replace(self.config, offline=False)
        failures = (
            TimeoutError("timed out"),
            urllib.error.URLError("DNS unavailable"),
            urllib.error.HTTPError(
                "https://github.com/Octo-Cat.png", 503, "Service Unavailable", {}, None
            ),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with patch("scripts.montage.urllib.request.urlopen", side_effect=failure):
                    with self.assertWarnsRegex(
                        UserWarning, r"@Octo-Cat.*failed.*using initials fallback"
                    ):
                        self.assertIsNone(fetch_avatar("Octo-Cat", online))

    def test_invalid_image_responses_warn_and_fallback(self):
        online = __import__("dataclasses").replace(self.config, offline=False)
        responses = (self.image_response(), self.image_response())
        responses[0].headers.get_content_type.return_value = "text/html"
        responses[1].read.return_value = b"not an image"
        for response in responses:
            with self.subTest(content_type=response.headers.get_content_type()):
                with patch("scripts.montage.urllib.request.urlopen", return_value=response):
                    with self.assertWarnsRegex(
                        UserWarning, r"@Octo-Cat.*failed.*using initials fallback"
                    ):
                        self.assertIsNone(fetch_avatar("Octo-Cat", online))

    def test_failed_refresh_retains_valid_cached_avatar(self):
        target = avatar_cache_path("Octo-Cat", self.config.cache_path)
        target.parent.mkdir()
        Image.new("RGB", (40, 40), "green").save(target)
        refreshing = __import__("dataclasses").replace(
            self.config, offline=False, refresh_avatars=True
        )
        with patch(
            "scripts.montage.urllib.request.urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertWarnsRegex(UserWarning, r"@Octo-Cat.*using cached avatar"):
                self.assertEqual(target, fetch_avatar("Octo-Cat", refreshing))
        with Image.open(target) as cached:
            self.assertEqual((0, 128, 0), cached.convert("RGB").getpixel((20, 20)))

    def test_renderer_has_safe_mirrored_positions_and_nonoverlap(self):
        avatar = Image.new("RGBA", (64, 64), "blue")
        left_path, right_path = self.root / "left.png", self.root / "right.png"
        left = render_lower_third(self.row(title="A Deliberately Long Beautiful Rainbow Game"),
                                  avatar, self.config, left_path)
        right = render_lower_third(self.row(position="bottom-right"), avatar, self.config, right_path)
        self.assertLessEqual(left.height, int(1080 * .15) + 20)  # includes soft-shadow padding
        self.assertEqual(60 - 10, left.x)
        self.assertEqual(1920 - 60 - 750 - 10, right.x)
        self.assertEqual(
            "A Deliberately Long Beautiful Rainbow Game", " ".join(left.title_lines)
        )
        self.assertGreater(left.username_bounds[2] - left.username_bounds[0], 0)
        self.assertLess(left.username_bounds[2], left.octocat_bounds[0])
        with Image.open(left_path) as image:
            self.assertEqual("RGBA", image.mode)
            self.assertGreater(image.getbbox()[2], 0)
            avatar_center = (
                (left.avatar_bounds[0] + left.avatar_bounds[2]) // 2,
                (left.avatar_bounds[1] + left.avatar_bounds[3]) // 2,
            )
            octocat_center = (
                (left.octocat_bounds[0] + left.octocat_bounds[2]) // 2,
                (left.octocat_bounds[1] + left.octocat_bounds[3]) // 2,
            )
            self.assertEqual((0, 0, 255), image.getpixel(avatar_center)[:3])
            self.assertEqual((255, 255, 255), image.getpixel(octocat_center)[:3])
            alpha = image.getchannel("A")
            shadow_pad = 10
            panel_right = shadow_pad + 750 - 1
            # The transparent rounded panel never writes into right shadow
            # padding. Only a low-alpha, blurred shadow may occupy that envelope.
            right_padding = alpha.crop((panel_right + 1, 0, image.width, image.height))
            self.assertLess(right_padding.getextrema()[1], 32)
            self.assertLessEqual(alpha.getpixel((panel_right, shadow_pad)), 2)
            self.assertLessEqual(alpha.getpixel((panel_right, shadow_pad + 149)), 2)
            self.assertEqual(0, alpha.getpixel((image.width - 1, 0)))
            self.assertEqual(0, alpha.getpixel((image.width - 1, image.height - 1)))

    def test_renderer_omits_all_placement_visuals(self):
        avatar = Image.new("RGBA", (64, 64), "blue")
        first_path = self.root / "first.png"
        thirteenth_path = self.root / "thirteenth.png"
        first = self.row()
        thirteenth = __import__("dataclasses").replace(first, place="13th", place_number=13)
        render_lower_third(first, avatar, self.config, first_path)
        render_lower_third(
            thirteenth, avatar, self.config, thirteenth_path
        )
        self.assertEqual(first_path.read_bytes(), thirteenth_path.read_bytes())


class InstagramConfigTests(unittest.TestCase):
    def config(self, **overrides):
        base = dict(
            input_dir=Path("."),
            csv_path=Path("data.csv"),
            output_path=Path("renders/output.mp4"),
            octocat_path=Path("logo.svg"),
            font_path=Path("font.ttf"),
            width=1920,
            height=1080,
            fps=30,
            transition_duration=Decimal("0.65"),
            keep_temp=False,
            force=False,
        )
        base.update(overrides)
        return MontageConfig(**base)

    def test_output_naming_and_enabled_flag(self):
        disabled = self.config()
        self.assertFalse(disabled.instagram_enabled)
        enabled = self.config(instagram_background=Path("bg.png"))
        self.assertTrue(enabled.instagram_enabled)
        self.assertEqual(Path("renders/output-instagram.mp4"), enabled.instagram_output_path)
        explicit = self.config(
            instagram_background=Path("bg.png"), instagram_output=Path("reels/story.mp4")
        )
        self.assertEqual(Path("reels/story.mp4"), explicit.instagram_output_path)

    def test_panel_height_is_even_and_16_by_9(self):
        self.assertEqual(608, instagram_panel_height(1080))
        self.assertEqual(0, instagram_panel_height(1080) % 2)

    def test_segment_overlays_full_width_gameplay_at_top(self):
        config = self.config(width=1080, height=1920, instagram_background=Path("bg.png"))
        media = MediaInfo(Path("1.mp4"), Decimal("13"), True, True, 1920, 1080, None)
        timing = calculate_cycle_timing([390], 30, Decimal("0.65"), Decimal("1.5"))
        row = __import__("scripts.montage", fromlist=["GameRow"]).GameRow(
            2, "1st", 1, "A", "@one", Path("1.mp4"), "one", "bottom-left"
        )
        command = build_instagram_segment_command(
            row, media, config, Path("card.png"), Path("seg.mkv"), timing=timing
        )
        joined = " ".join(command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1080:608", graph)
        self.assertIn("overlay=x=0:y=0", graph)
        self.assertIn("[1:v]format=rgba", graph)
        self.assertIn("trim=end_frame=390", graph)
        self.assertIn("-map [v] -map [a]", joined)
        self.assertIn("pcm_s16le", joined)
        self.assertEqual("1.mp4", command[command.index("-i") + 1])
        self.assertIn("card.png", joined)

    def test_combine_graph_is_resolution_independent(self):
        with tempfile.TemporaryDirectory(dir=".") as temp:
            root = Path(temp)
            segments = [root / f"{index}.mkv" for index in range(2)]
            durations = [Decimal("3.25"), Decimal("5.5")]
            landscape = self.config(width=1920, height=1080, output_path=root / "out.mp4")
            portrait = self.config(width=1080, height=1920, output_path=root / "out-instagram.mp4")
            land_filter = root / "land.txt"
            port_filter = root / "port.txt"
            build_combine_command(segments, durations, landscape, land_filter, root / "a.mp4")
            build_combine_command(segments, durations, portrait, port_filter, root / "b.mp4")
            self.assertEqual(
                land_filter.read_text(encoding="utf-8"), port_filter.read_text(encoding="utf-8")
            )


class InstagramCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name)
        font = Path(os.environ.get("MONTAGE_TEST_FONT", "/Library/Fonts/MonaSans-Regular.ttf"))
        bold = Path(os.environ.get("MONTAGE_TEST_FONT_BOLD", "/Library/Fonts/MonaSans-Bold.ttf"))
        if not font.is_file() or not bold.is_file():
            self.skipTest("Mona Sans test fonts unavailable")
        logo = self.root / "logo.png"
        Image.new("RGBA", (32, 32), "white").save(logo)
        self.background = self.root / "bg.png"
        Image.new("RGB", (1200, 1600), (20, 40, 90)).save(self.background)
        self.config = MontageConfig(
            self.root, self.root / "data.csv", self.root / "out.mp4", logo, font,
            1080, 1920, 30, Decimal(".65"), False, False,
            avatar_cache=self.root / "cache", offline=True, font_bold_path=bold,
            instagram_background=self.background, hubot_font_path=bold,
            instagram_width=1080, instagram_height=1920,
        )

    def tearDown(self):
        self.temp.cleanup()

    def row(self, *, title="A Deliberately Long Rainbow Unicorn Game", place="1st"):
        return __import__("scripts.montage", fromlist=["GameRow"]).GameRow(
            2, place, 1, title, "@Octo-Cat", self.root / "1.mp4", "Octo-Cat", "bottom-left"
        )

    def test_card_geometry_fonts_logo_and_footer_alignment(self):
        avatar = Image.new("RGBA", (108, 108), "blue")
        out = self.root / "card.png"
        layout = render_instagram_card(self.row(), avatar, self.config, out)
        self.assertEqual((1080, 1920), (layout.width, layout.height))
        self.assertEqual(608, layout.panel_height)
        self.assertEqual((0, 608, 1080, 1920), layout.background_bounds)
        # Metadata sits in the lower region, below the gameplay panel.
        for bounds in (layout.place_bounds, layout.title_bounds, layout.avatar_bounds,
                       layout.creator_bounds):
            self.assertGreaterEqual(bounds[1], layout.panel_height)
        # Wordmark bottom-left, Invertocat bottom-right at a shared margin.
        self.assertEqual(layout.wordmark_bounds[0], 1080 - layout.octocat_bounds[2])
        self.assertLess(layout.wordmark_bounds[2], layout.octocat_bounds[0])
        # Matching visual height and baseline.
        wordmark_height = layout.wordmark_bounds[3] - layout.wordmark_bounds[1]
        octocat_height = layout.octocat_bounds[3] - layout.octocat_bounds[1]
        self.assertLessEqual(abs(wordmark_height - octocat_height), 2)
        self.assertLessEqual(abs(layout.wordmark_bounds[3] - layout.octocat_bounds[3]), 2)
        self.assertEqual(
            "A Deliberately Long Rainbow Unicorn Game", " ".join(layout.title_lines)
        )
        with Image.open(out) as image:
            self.assertEqual((1080, 1920), image.size)
            avatar_center = (
                (layout.avatar_bounds[0] + layout.avatar_bounds[2]) // 2,
                (layout.avatar_bounds[1] + layout.avatar_bounds[3]) // 2,
            )
            self.assertEqual((0, 0, 255), image.convert("RGB").getpixel(avatar_center))
            octocat_center = (
                (layout.octocat_bounds[0] + layout.octocat_bounds[2]) // 2,
                (layout.octocat_bounds[1] + layout.octocat_bounds[3]) // 2,
            )
            self.assertEqual((255, 255, 255), image.convert("RGB").getpixel(octocat_center))

    def test_card_rejects_undecodable_background(self):
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        config = __import__("dataclasses").replace(self.config, instagram_background=broken)
        avatar = Image.new("RGBA", (108, 108), "blue")
        with self.assertRaisesRegex(MontageError, "background"):
            render_instagram_card(self.row(), avatar, config, self.root / "card.png")


class ValidateInputsCollisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name)
        self.octocat = self.root / "logo.svg"
        self.font = self.root / "font.ttf"
        self.octocat.write_text("<svg/>", encoding="utf-8")
        self.font.write_bytes(b"\x00")
        self.background = self.root / "bg.png"
        Image.new("RGB", (64, 64), (10, 20, 30)).save(self.background)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, instagram_output):
        return MontageConfig(
            self.root, self.root / "data.csv", self.root / "out.mp4", self.octocat, self.font,
            1920, 1080, 30, Decimal(".65"), False, False,
            font_bold_path=self.font, instagram_background=self.background,
            hubot_font_path=self.font, instagram_output=instagram_output,
        )

    def test_instagram_output_collides_with_auxiliary_artifacts(self):
        cases = {
            "the lower-third preview": self.root / "lower-third-preview.png",
            "the contact sheet": self.root / "montage-contact-sheet.jpg",
            "the loop-test video": self.root / "loop-test.mp4",
        }
        for label, artifact in cases.items():
            with self.subTest(artifact=artifact.name):
                config = self.config(artifact)
                with self.assertRaisesRegex(MontageError, label):
                    validate_inputs(config, [])


    def test_instagram_output_rejects_case_only_aliases(self):
        cases = {
            "the landscape output path": self.root / "OUT.mp4",
            "the lower-third preview": self.root / "Lower-Third-Preview.png",
            "the contact sheet": self.root / "Montage-Contact-Sheet.jpg",
            "the loop-test video": self.root / "Loop-Test.mp4",
        }
        with patch("scripts.montage._case_insensitive_target", return_value=True):
            for label, alias in cases.items():
                with self.subTest(artifact=alias.name):
                    config = self.config(alias)
                    with self.assertRaisesRegex(MontageError, label):
                        validate_inputs(config, [])

    def test_case_only_alias_ignored_on_case_sensitive_target(self):
        with patch("scripts.montage._case_insensitive_target", return_value=False):
            self.assertFalse(paths_collide(self.root / "out.mp4", self.root / "OUT.mp4"))

    def test_paths_collide_detects_case_only_and_identical(self):
        with patch("scripts.montage._case_insensitive_target", return_value=True):
            self.assertTrue(paths_collide(self.root / "out.mp4", self.root / "OUT.mp4"))
        self.assertTrue(paths_collide(self.root / "out.mp4", self.root / "sub" / ".." / "out.mp4"))
        self.assertFalse(paths_collide(self.root / "out.mp4", self.root / "other.mp4"))


if __name__ == "__main__":
    unittest.main()
