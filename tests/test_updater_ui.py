# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""安装器 UI 单测（需求 §43，调研报告 §11.2/§11.4）。

**不启动 GUI、不依赖 tkinter**（本机没有 tkinter，CI 的 Linux 也未必有）：

- 调色板逐值与 ``mangaproof/ui/theme.py:18-32`` 对齐 —— 通过**读源码文本**校验，
  绝不 import 那个模块（它依赖 PySide6）；
- 控制台降级必须呈现"阶段 + 具体文件 + 计数"三层信息；
- ``stdout``/``stderr`` 为 ``None``（windowed onefile）时不得抛异常；
- tkinter 缺失时自动降级，字体缺失时退回 Tk 默认字体且不崩。
"""

from __future__ import annotations

import io
import logging
import re
import sys
from pathlib import Path

import pytest

from updater import ui

ROOT = Path(__file__).parent.parent
THEME_SOURCE = ROOT / "mangaproof" / "ui" / "theme.py"

#: 语义 → 期待色值（调研报告 §11.2 的逐值表）
PALETTE_SPEC = {
    "COLOR_BG_MAIN": "#2b2d30",
    "COLOR_BG_PANEL": "#313438",
    "COLOR_BG_WIDGET": "#3a3d42",
    "COLOR_BG_HOVER": "#45494f",
    "COLOR_BG_SELECTED": "#2d5f8a",
    "COLOR_BORDER": "#4a4e54",
    "COLOR_TEXT": "#e4e6eb",
    "COLOR_TEXT_DIM": "#a0a4ab",
    "COLOR_ACCENT": "#4a90d9",
    "COLOR_PASS": "#4caf50",
    "COLOR_FAIL": "#e53935",
    "COLOR_WARN": "#f5a623",
}


def _theme_colors_from_source() -> dict[str, str]:
    text = THEME_SOURCE.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r'^(COLOR_[A-Z_]+)\s*=\s*"([^"]+)"', line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def test_palette_matches_theme_source_exactly():
    theme = _theme_colors_from_source()
    for name, expected in PALETTE_SPEC.items():
        assert theme.get(name) == expected, f"theme.py 里的 {name} 变了，安装器需同步"
        assert getattr(ui, name) == expected, f"updater/ui.py 的 {name} 与规格不符"


def test_ui_source_documents_theme_line_numbers():
    """规格要求"把色值抄成常量并注明来源行号"（调研报告 §11.2）。"""
    text = (ROOT / "updater" / "ui.py").read_text(encoding="utf-8")
    assert "theme.py:18" in text and "theme.py:29" in text


def test_font_fallback_order_matches_spec():
    assert ui.FONT_CANDIDATES == (
        "MiSans",
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Noto Sans CJK SC",
        "PingFang SC",
    )


def test_pick_font_family_never_raises():
    """没有 tkinter / 没有 Tk() 时也只能返回 None（用 Tk 默认字体），不得抛错。"""
    assert ui.pick_font_family(None) is None


def test_console_reporter_renders_three_layers():
    stream = io.StringIO()
    reporter = ui.ConsoleReporter(stream)
    reporter.phase("RESTORE_DATA")
    reporter.item("logs/2026-09-22.log", "恢复")
    reporter.progress(3, 12)
    reporter.log("已恢复 3 项")
    reporter.finish(False, "测试失败")

    text = stream.getvalue()
    assert "正在恢复用户数据" in text          # ① 阶段
    assert "恢复：logs/2026-09-22.log" in text  # ② 具体文件
    assert "3/12" in text                      # ③ 计数
    assert "更新失败" in text and "测试失败" in text
    assert reporter.finished is True
    assert reporter.lines


def test_console_reporter_tolerates_missing_stdout(monkeypatch):
    """windowed onefile 下 sys.stdout/stderr 可能是 None（需求 §43 的构建形态）。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    reporter = ui.ConsoleReporter(None)
    reporter.phase("EXTRACT")
    reporter.item("MangaProof/_internal/x.so", "解压")
    reporter.progress(1, 2)
    reporter.progress(-1, -1)
    reporter.finish(True, "")
    assert any("解压" in line for line in reporter.lines)


def test_console_reporter_tolerates_broken_stream():
    class Broken:
        def write(self, _text):
            raise ValueError("closed")

        def flush(self):
            raise ValueError("closed")

        def isatty(self):
            return False

    reporter = ui.ConsoleReporter(Broken())
    reporter.phase("VALIDATE")  # 不得抛异常
    assert reporter.lines


def test_progress_with_unknown_total_is_accepted():
    reporter = ui.ConsoleReporter(io.StringIO())
    reporter.progress(0, -1)
    reporter.progress(5, None)  # type: ignore[arg-type]
    assert reporter.finished is False


def test_make_reporter_cli_returns_console_backed_reporter():
    reporter = ui.make_reporter(cli=True, stream=io.StringIO())
    assert isinstance(reporter, ui.MultiReporter)
    reporter.phase("INIT")
    reporter.finish(True, "")


def test_run_with_ui_degrades_to_console_when_tkinter_missing(monkeypatch):
    monkeypatch.setattr(ui, "tkinter_unavailable_reason", lambda: "测试：没有 tkinter")

    def work(reporter):
        reporter.phase("INIT")
        reporter.item("settings.json", "备份")
        return 0

    assert ui.run_with_ui(work, cli=False) == 0


def test_run_with_ui_cli_mode_returns_exit_code():
    def work(reporter):
        reporter.phase("INIT")
        reporter.finish(False, "boom")
        return 42

    assert ui.run_with_ui(work, cli=True) == 42


def test_run_with_ui_finishes_when_work_forgets(monkeypatch):
    monkeypatch.setattr(ui, "tkinter_unavailable_reason", lambda: "no tk")
    reporter_holder: list = []

    def work(reporter):
        reporter_holder.append(reporter)
        return 7

    assert ui.run_with_ui(work) == 7
    assert reporter_holder[0].finished is True


def test_run_with_ui_handles_work_exception(monkeypatch):
    monkeypatch.setattr(ui, "tkinter_unavailable_reason", lambda: "no tk")

    def work(reporter):
        raise RuntimeError("内核炸了")

    assert ui.run_with_ui(work) == 90  # INTERNAL


def test_logging_reporter_writes_to_logger(caplog):
    reporter = ui.LoggingReporter(logging.getLogger("test.updater.ui"))
    with caplog.at_level(logging.INFO):
        reporter.phase("ROLLBACK")
        reporter.item("MangaProof.old → MangaProof", "回滚")
        reporter.progress(1, 3)
    assert any("ROLLBACK" in record.getMessage() for record in caplog.records)
    assert any("回滚" in record.getMessage() for record in caplog.records)


def test_null_reporter_accepts_everything():
    reporter = ui.NullReporter()
    reporter.phase("X")
    reporter.item("a")
    reporter.progress(1, 1)
    reporter.log("x")
    reporter.finish(True)
    assert reporter.finished


def test_tkinter_availability_is_consistent():
    reason = ui.tkinter_unavailable_reason()
    assert ui.tkinter_available() is (reason is None)
    if reason is not None:
        assert "tkinter" in reason or "图形" in reason


@pytest.mark.skipif(ui.tkinter_available(), reason="本机有 tkinter 时这一步没有意义")
def test_missing_tkinter_reason_is_recorded():
    assert ui.tkinter_unavailable_reason() is not None
