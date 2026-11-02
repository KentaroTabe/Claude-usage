"""HTML のレポートを PDF にする。

macOS 標準の WebKit を使う小さな Swift のヘルパー(html2pdf.swift)を初回にビルドして呼ぶ。
追加のパッケージやブラウザは使わない。ビルドには Xcode のコマンドラインツール(xcrun swiftc)が必要。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, tzinfo
from pathlib import Path

SWIFT_SOURCE = Path(__file__).resolve().with_name("html2pdf.swift")


class PdfError(Exception):
    pass


def report_pdf_path(pdf_dir: Path, week_end: datetime, tz: tzinfo) -> Path:
    """毎週のレポートの保存先。ファイル名はリセット日(その週の終わり)。"""
    return pdf_dir / f"Claude_Usage_{week_end.astimezone(tz):%Y-%m-%d}.pdf"


def helper_needs_build(source: Path, binary: Path) -> bool:
    return not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime


def build_helper(binary: Path, source: Path = SWIFT_SOURCE) -> Path:
    if not helper_needs_build(source, binary):
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["xcrun", "swiftc", "-O", str(source), "-o", str(binary)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise PdfError(f"PDF 用ヘルパーのビルドに失敗しました: {result.stderr.strip()[-2000:]}")
    return binary


def html_to_pdf(html: Path, pdf: Path, helper: Path, timeout_seconds: float) -> Path:
    """一時ファイルに書いてから置き換える(途中で失敗しても壊れた PDF を残さないため)。

    ヘルパー自身にも同じ待ち時間の上限を渡し、どちらが先に時間切れになっても PdfError にする。
    """
    pdf = pdf.resolve()
    pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pdf.with_name(f".{pdf.stem}.tmp.pdf")
    try:
        result = subprocess.run([str(helper), str(html.resolve()), str(tmp), str(timeout_seconds)],
                                capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise PdfError(f"PDF の作成が {timeout_seconds} 秒で終わりませんでした") from None
    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise PdfError(f"PDF の作成に失敗しました(終了コード {result.returncode}): {result.stderr.strip()[-2000:]}")
    os.replace(tmp, pdf)
    return pdf
