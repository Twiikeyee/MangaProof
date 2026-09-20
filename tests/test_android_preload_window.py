# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MainWindow 层面的预加载/驱逐窗口分档测试。

补 task_loader 之外的另两处落点：_schedule_preloads 的候选队列与
_current_keep_set 的驱逐保留窗口，都必须与平台窗口同源。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_android_preload_window.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import mangaproof.config.settings as settings_mod
import mangaproof.utils.platform as platform_mod
from mangaproof.config.settings import (
    PRELOAD_WINDOW_OFFSETS_ANDROID,
    PRELOAD_WINDOW_OFFSETS_DESKTOP,
    SettingsManager,
)
from mangaproof.review.state import FileRecord, TaskState
from mangaproof.utils.platform import is_android_strict

_ON_ANDROID = is_android_strict()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _as_android(monkeypatch):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: True)


def _as_desktop(monkeypatch):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: False)


def _make_task(n=10, current_index=4):
    t = TaskState()
    for i in range(n):
        rel = f"p{i:02d}.psd"
        t.files.append(FileRecord(relative_path=rel, file_name=rel, size=1))
    t.current_file = t.files[current_index].relative_path
    return t


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_current_keep_set_is_platform_scoped(qapp, monkeypatch, tmp_path):
    """驱逐保留窗口：Android 3 页 / 桌面 6 页。"""
    from mangaproof.ui.main_window import MainWindow

    window = MainWindow(SettingsManager(tmp_path / "settings.json"))
    try:
        window.task = _make_task()
        window._current_file = "p04.psd"      # _current_keep_set 要求它非空

        _as_android(monkeypatch)
        assert window._current_keep_set() == {"p03.psd", "p04.psd", "p05.psd"}

        _as_desktop(monkeypatch)
        assert window._current_keep_set() == {
            "p02.psd", "p03.psd", "p04.psd", "p05.psd", "p06.psd", "p07.psd"
        }
        # 窗口内文档（p04）被驱逐保护，窗口外不受保护
        keep = window._current_keep_set()
        assert "p04.psd" in keep
        assert "p00.psd" not in keep
    finally:
        monkeypatch.undo()     # 恢复真实平台判定，closeEvent 才按真实平台收尾
        window.close()


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_schedule_preloads_queue_is_platform_scoped(qapp, monkeypatch, tmp_path):
    """预热队列：Android 排 3 个文件（前1/当前/后1），桌面排 5 个。"""
    from mangaproof.ui import main_window as mw

    window = mw.MainWindow(SettingsManager(tmp_path / "settings.json"))
    mp = pytest.MonkeyPatch()
    try:
        window.task = _make_task()
        window._current_file = "p04.psd"
        window._base_dir = tmp_path

        # 用假文档替代真实 PSD：has_bg 返回 False → 每个候选都会被排进
        # 阶段 B（图层预热），这正是我们要数的东西。
        class _FakeDoc:
            all_layers_warmed = True          # 让 _all_layers_warm 直接判就绪
            def has_merged(self):
                return False
            def has_bg(self):
                return False

        captured = {}

        class _FakeWorker:
            def set_preloads(self, merged, extra):
                captured["merged"] = list(merged)
                captured["extra"] = list(extra)
            def stop(self):                   # closeEvent 会调用
                pass

        mp.setattr(window, "_ensure_doc", lambda rel: _FakeDoc())
        mp.setattr(window, "_preload", _FakeWorker())
        mp.setattr(mw.MainWindow, "_evict_outside_window", lambda self, keep: None)

        _as_android(monkeypatch)
        window._schedule_preloads("p04.psd")
        got = {rel for rel, _ in captured["extra"]}
        assert got == {"p03.psd", "p04.psd", "p05.psd"}, got
        assert window._keep_set == got

        _as_desktop(monkeypatch)
        window._schedule_preloads("p04.psd")
        got = {rel for rel, _ in captured["extra"]}
        assert got == {"p02.psd", "p03.psd", "p04.psd",
                       "p05.psd", "p06.psd", "p07.psd"}, got
    finally:
        mp.undo()          # 先恢复真实 _preload，closeEvent 才能停掉 QThread
        window.close()


def test_offset_tables_match_documented_ranges():
    """偏移表本身的形状：桌面 6 项含前 2 松弛；Android 严格 3 项。"""
    assert PRELOAD_WINDOW_OFFSETS_ANDROID == (-1, 0, 1)
    assert sorted(PRELOAD_WINDOW_OFFSETS_DESKTOP) == [-2, -1, 0, 1, 2, 3]
    assert 0 in PRELOAD_WINDOW_OFFSETS_ANDROID
    assert 0 in PRELOAD_WINDOW_OFFSETS_DESKTOP
