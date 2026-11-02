import os
import stat
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_usage.render.pdf import PdfError, helper_needs_build, html_to_pdf, report_pdf_path


def fake_helper(directory: Path, body: str) -> Path:
    """本物の WebKit の代わりに、引数を受け取って決まった動作をするだけのスクリプト。"""
    path = directory / "fake_html2pdf"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class ReportPdfPathTest(unittest.TestCase):
    def test_named_by_reset_date_in_local_time(self):
        end = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)  # 10/27 19:00 JST
        self.assertEqual(report_pdf_path(Path("/docs"), end, ZoneInfo("Asia/Tokyo")),
                         Path("/docs/Claude_Usage_2026-10-27.pdf"))

    def test_date_changes_across_midnight(self):
        end = datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)  # 10/28 01:00 JST
        self.assertEqual(report_pdf_path(Path("/docs"), end, ZoneInfo("Asia/Tokyo")).name,
                         "Claude_Usage_2026-10-28.pdf")


class HelperNeedsBuildTest(unittest.TestCase):
    def test_missing_older_newer(self):
        with tempfile.TemporaryDirectory() as d:
            source, binary = Path(d) / "a.swift", Path(d) / "a"
            source.write_text("//")
            self.assertTrue(helper_needs_build(source, binary))
            binary.write_text("bin")
            os.utime(binary, (time.time() - 100, time.time() - 100))
            self.assertTrue(helper_needs_build(source, binary))
            os.utime(binary, (time.time() + 100, time.time() + 100))
            self.assertFalse(helper_needs_build(source, binary))


class HtmlToPdfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.html = self.dir / "r.html"
        self.html.write_text("<p>x</p>")

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_writes_target_atomically(self):
        helper = fake_helper(self.dir, 'printf "%%PDF-1.4 from %s" "$1" > "$2"')
        out = html_to_pdf(self.html, self.dir / "sub" / "r.pdf", helper, 5)
        self.assertEqual(out, (self.dir / "sub" / "r.pdf").resolve())
        self.assertTrue(out.read_text().startswith("%PDF-1.4 from /"))  # 入力は絶対パスで渡す
        self.assertEqual([p.name for p in out.parent.iterdir()], ["r.pdf"])  # 一時ファイルは残らない

    def test_failure_keeps_previous_pdf_and_reports_stderr(self):
        target = self.dir / "r.pdf"
        target.write_text("old")
        helper = fake_helper(self.dir, 'echo "boom" >&2; : > "$2"; exit 2')
        with self.assertRaisesRegex(PdfError, "boom"):
            html_to_pdf(self.html, target, helper, 5)
        self.assertEqual(target.read_text(), "old")
        self.assertFalse((self.dir / ".r.tmp.pdf").exists())

    def test_timeout(self):
        helper = fake_helper(self.dir, "sleep 30")
        with self.assertRaisesRegex(PdfError, "終わりませんでした"):
            html_to_pdf(self.html, self.dir / "r.pdf", helper, 0.5)
        self.assertFalse((self.dir / "r.pdf").exists())


if __name__ == "__main__":
    unittest.main()
