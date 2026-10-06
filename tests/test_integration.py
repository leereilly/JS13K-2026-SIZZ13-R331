import csv
import os
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe") and shutil.which("rsvg-convert"), "media tools unavailable")
class MontageIntegrationTest(unittest.TestCase):
    def test_mixed_media_end_to_end(self):
        font = os.environ.get("MONTAGE_TEST_FONT", "/System/Library/Fonts/Supplemental/Verdana.ttf")
        if not Path(font).is_file():
            self.skipTest("set MONTAGE_TEST_FONT to an installed TTF/OTF font")
        with tempfile.TemporaryDirectory(dir=".") as temp:
            root = Path(temp)
            clips = root / "clips"
            clips.mkdir()
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=1.2", "-f", "lavfi", "-i", "sine=d=1.2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clips / "1.mp4")], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=1.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clips / "2.mp4")], check=True)
            logo = root / "logo.svg"
            logo.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><circle cx="32" cy="32" r="30" fill="white"/></svg>', encoding="utf-8")
            data = root / "data.csv"
            with data.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Place", "Game title", "GitHub usernames with @ that created it"])
                writer.writerows([["2nd", "Portrait, first", "two"], ["1st", "Landscape", "@one"]])
            output = root / "out.mp4"
            subprocess.run(["./make-montage.sh", str(clips), str(data), str(output), "--octocat", str(logo), "--font", font, "--width", "640", "--height", "360", "--transition-duration", "0.2", "--preset", "ultrafast"], check=True)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels", "-of", "default=nw=1", str(output)], check=True, text=True, capture_output=True).stdout
            for expected in ("codec_name=h264", "width=640", "height=360", "pix_fmt=yuv420p", "r_frame_rate=30/1", "codec_name=aac", "sample_rate=48000", "channels=2"):
                self.assertIn(expected, probe)
            duration = Decimal(
                subprocess.run(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=nw=1:nk=1",
                        str(output),
                    ],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
            )
            self.assertAlmostEqual(2.2, float(duration), delta=0.1)


if __name__ == "__main__":
    unittest.main()
