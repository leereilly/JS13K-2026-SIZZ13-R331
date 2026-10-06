import csv
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from scripts.montage import (
    MediaInfo,
    MontageConfig,
    build_combine_command,
    calculate_transition_offsets,
    normalize_username,
    parse_csv,
    parse_place_number,
    probe_media,
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

    def test_combine_graph_uses_offset_xfade_chain_without_trimming(self):
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

        self.assertIn(
            "[0:v][1:v]xfade=transition=fade:duration=0.75:offset=2.5[v1]",
            graph,
        )
        self.assertIn(
            "[v1][2:v]xfade=transition=fade:duration=0.75:offset=7.25[vout]",
            graph,
        )
        self.assertIn("[0:a][1:a]acrossfade=d=0.75:c1=tri:c2=tri[a1]", graph)
        self.assertIn("[a1][2:a]acrossfade=d=0.75:c1=tri:c2=tri[a2]", graph)
        self.assertNotIn("trim=", graph)
        self.assertNotIn("concat=", graph)


if __name__ == "__main__":
    unittest.main()
