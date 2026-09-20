# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Android 内存策略锁定为激进的测试。

需求：Python 侧，安卓端的内存回收策略只允许「激进」；桌面端三档不变。

覆盖：
- 读到非激进档（旧版本写的 / 手工编辑的）→ 强制激进，并把值写回 settings.json
  （文件与运行值一致，不留一个永不生效的旧档位）；
- 非法值 / 缺键 → 回落**平台默认**（Android 激进、桌面平衡）；
- 桌面完全不受影响：读到什么是什么、非法回落 balanced、reconcile 不落盘；
- 设置页：Android 上该控件禁用但保留（看得见当前档位、改不了）；
- 首次运行横幅：Android 文案不含"内存策略"，桌面含。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_android_memory_policy.py -v
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import mangaproof.config.settings as settings_mod
import mangaproof.utils.platform as platform_mod
from mangaproof.config.settings import (
    ANDROID_MEMORY_POLICY,
    DEFAULT_MEMORY_POLICY,
    PRELOAD_WINDOW_OFFSETS_ANDROID,
    PRELOAD_WINDOW_OFFSETS_DESKTOP,
    Settings,
    SettingsManager,
    android_memory_policy_locked,
    default_memory_policy,
    effective_memory_policy,
    first_run_banner,
    preload_window_offsets,
    reconcile_android_memory_policy,
)
from mangaproof.ui.task_loader import _window_set
from mangaproof.utils.platform import is_android_strict

_ON_ANDROID = is_android_strict()


def _as_android(monkeypatch) -> None:
    """把"当前平台"伪装成 Android。

    is_android_strict 是在各模块里按名字导入的，所以要两处都打：config.settings
    的判定入口，以及 utils.platform 本体（万一有别的调用方）。
    """
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: True)


def _as_desktop(monkeypatch) -> None:
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: False)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 纯逻辑：平台判定 / 默认值 / 规整
# ---------------------------------------------------------------------------

def test_android_locks_and_desktop_does_not(monkeypatch):
    _as_android(monkeypatch)
    assert android_memory_policy_locked() is True
    assert default_memory_policy() == ANDROID_MEMORY_POLICY
    _as_desktop(monkeypatch)
    assert android_memory_policy_locked() is False
    assert default_memory_policy() == DEFAULT_MEMORY_POLICY


def test_effective_policy_regularizes(monkeypatch):
    """有效档位的规整口径：Android 一律激进；桌面非法值回落 balanced。"""
    _as_android(monkeypatch)
    for configured in ("relaxed", "balanced", "aggressive", "turbo", None, 42):
        assert effective_memory_policy(configured) == ANDROID_MEMORY_POLICY
    _as_desktop(monkeypatch)
    assert effective_memory_policy("relaxed") == "relaxed"
    assert effective_memory_policy("balanced") == "balanced"
    assert effective_memory_policy("aggressive") == "aggressive"
    for bad in ("turbo", None, 42, ""):
        assert effective_memory_policy(bad) == DEFAULT_MEMORY_POLICY


# ---------------------------------------------------------------------------
# 读设置：强制 + 写回
# ---------------------------------------------------------------------------

@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_android_forced_and_written_back(monkeypatch, tmp_path):
    """旧配置里的 balanced → 读到 aggressive，且值被写回文件。"""
    _as_android(monkeypatch)
    path = tmp_path / "settings.json"
    _write(path, {"settings_version": 1, "memory_policy": "balanced"})

    manager = SettingsManager(path)
    assert manager.settings.memory_policy == "aggressive", "读取时就要强制"

    assert reconcile_android_memory_policy(manager) == "aggressive"
    assert json.loads(path.read_text(encoding="utf-8"))["memory_policy"] == "aggressive"

    # 幂等：文件已经对了就不再落盘（比较 mtime 不足以证明，直接看返回值）
    assert reconcile_android_memory_policy(manager) is None

    # 下一次启动读到的一致（不再需要纠正）
    assert SettingsManager(path).settings.memory_policy == "aggressive"


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_android_invalid_value_falls_back_to_aggressive(monkeypatch, tmp_path):
    """非法值在 Android 上必须回落 aggressive，而不是 balanced。"""
    _as_android(monkeypatch)
    path = tmp_path / "settings.json"
    _write(path, {"settings_version": 1, "memory_policy": "turbo"})
    assert SettingsManager(path).settings.memory_policy == "aggressive"


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_android_missing_key_gives_aggressive(monkeypatch, tmp_path):
    _as_android(monkeypatch)
    path = tmp_path / "settings.json"
    _write(path, {"settings_version": 1})
    assert SettingsManager(path).settings.memory_policy == "aggressive"


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_desktop_untouched_by_reconcile(monkeypatch, tmp_path):
    """桌面端：读到什么是什么；reconcile 不碰文件；非法值回落 balanced。"""
    _as_desktop(monkeypatch)
    path = tmp_path / "settings.json"
    _write(
        path,
        {"settings_version": 1, "memory_policy": "relaxed", "layer_display_ratio": 0.7},
    )

    manager = SettingsManager(path)
    assert manager.settings.memory_policy == "relaxed"
    assert reconcile_android_memory_policy(manager) is None

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["memory_policy"] == "relaxed", "桌面端不得被改写"
    assert "aggressive" not in path.read_text(encoding="utf-8")

    _write(path, {"settings_version": 1, "memory_policy": "turbo"})
    assert SettingsManager(path).settings.memory_policy == DEFAULT_MEMORY_POLICY


def test_reconcile_uses_managers_own_path(monkeypatch, tmp_path):
    """回归：写回必须落在 manager 自己的路径，且首次运行不凭空建文件。

    照抄 reconcile_android_ui_scale 用 paths.settings_path() 会写到真实程序目录，
    测试注入的 tmp_path 反被绕过。
    """
    _as_android(monkeypatch)
    path = tmp_path / "settings.json"
    manager = SettingsManager(path)
    # 首次运行：文件不存在 → 内存值已是默认激进，没有旧值可纠正，不落盘
    assert reconcile_android_memory_policy(manager) is None
    assert not path.exists(), "首次运行不该凭空创建 settings.json（会让提醒条立刻收起）"

    # 文件存在且档位不对 → 纠正，且落在同一个路径
    manager.save()
    manager.settings.memory_policy = "relaxed"
    assert reconcile_android_memory_policy(manager) == "aggressive"
    assert json.loads(path.read_text(encoding="utf-8"))["memory_policy"] == "aggressive"
    assert reconcile_android_memory_policy(manager) is None, "已一致 → 幂等"


# ---------------------------------------------------------------------------
# 设置页：禁用但保留
# ---------------------------------------------------------------------------

def _dialog(settings: Settings):
    from mangaproof.ui.settings_dialog import SettingsDialog

    return SettingsDialog(settings, None)


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_dialog_disables_combo_on_android(qapp, monkeypatch):
    _as_android(monkeypatch)
    dialog = _dialog(Settings(memory_policy="aggressive"))
    try:
        combo = dialog.memory_policy_combo
        assert combo.isEnabled() is False, "Android 上不允许改档位"
        assert combo.currentData() == ANDROID_MEMORY_POLICY
        assert combo.itemText(combo.currentIndex()).startswith("激进")
        assert "固定" in combo.toolTip(), "要说明为什么不能改"

        # 「恢复默认」复位到平台默认（激进），不能把灰控件显示成「平衡」
        dialog._reset_defaults()
        assert combo.currentData() == ANDROID_MEMORY_POLICY

        # 即使控件被绕过（例如直接改下拉），写回设置的值仍是激进
        combo.setCurrentIndex(combo.findData("relaxed"))
        s = Settings()
        dialog.apply_to(s)
        assert s.memory_policy == "aggressive"
    finally:
        dialog.deleteLater()


@pytest.mark.skipif(_ON_ANDROID, reason="本机就是 Android，无法模拟桌面端")
def test_dialog_keeps_three_choices_on_desktop(qapp, monkeypatch):
    _as_desktop(monkeypatch)
    dialog = _dialog(Settings(memory_policy="relaxed"))
    try:
        combo = dialog.memory_policy_combo
        assert combo.isEnabled() is True
        assert combo.count() == 3, "桌面三档不变"
        assert combo.currentData() == "relaxed"
        dialog._reset_defaults()
        assert combo.currentData() == DEFAULT_MEMORY_POLICY
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# 首次运行横幅文案
# ---------------------------------------------------------------------------

def test_first_run_banner_is_platform_aware(monkeypatch):
    _as_android(monkeypatch)
    android_text = first_run_banner()
    assert "内存策略" not in android_text, "Android 上该项不可调，文案不该提它"
    assert "界面缩放" in android_text

    _as_desktop(monkeypatch)
    assert "内存策略" in first_run_banner(), "桌面端仍可调，文案保留"


# ---------------------------------------------------------------------------
# 预加载 / 保留窗口：Android 3 页，桌面 6 页
# ---------------------------------------------------------------------------

def _task(rels, current):
    from mangaproof.review.state import FileRecord, TaskState

    t = TaskState()
    for rel in rels:
        t.files.append(FileRecord(relative_path=rel, file_name=rel, size=1))
    t.current_file = current
    return t


def _pages(n, current, offsets):
    return {r for r in _window_set(_task([f"p{i:02d}.psd" for i in range(n)], current),
                                   offsets=offsets)}


def test_window_offsets_by_platform(monkeypatch):
    _as_android(monkeypatch)
    assert preload_window_offsets() == PRELOAD_WINDOW_OFFSETS_ANDROID
    assert preload_window_offsets() == (-1, 0, 1)
    _as_desktop(monkeypatch)
    assert preload_window_offsets() == PRELOAD_WINDOW_OFFSETS_DESKTOP
    # 桌面取值必须与改动前的硬编码完全一致：后3 + 前1 + 前2 松弛
    assert sorted(PRELOAD_WINDOW_OFFSETS_DESKTOP) == [-2, -1, 0, 1, 2, 3]


def test_android_window_is_three_pages():
    """Android：前 1 + 当前 + 后 1，不含额外松弛。"""
    keep = _pages(10, "p04.psd", PRELOAD_WINDOW_OFFSETS_ANDROID)
    assert keep == {"p03.psd", "p04.psd", "p05.psd"}


def test_desktop_window_unchanged():
    """桌面：仍是 6 页（含前 2 松弛），与改动前逐项一致。"""
    keep = _pages(10, "p04.psd", PRELOAD_WINDOW_OFFSETS_DESKTOP)
    assert keep == {"p02.psd", "p03.psd", "p04.psd",
                    "p05.psd", "p06.psd", "p07.psd"}


@pytest.mark.parametrize("offsets", [PRELOAD_WINDOW_OFFSETS_ANDROID,
                                     PRELOAD_WINDOW_OFFSETS_DESKTOP])
@pytest.mark.parametrize("index,current", [(0, "p00.psd"), (4, "p04.psd"), (9, "p09.psd")])
def test_window_clips_at_book_edges(offsets, index, current):
    """书首/书尾：只裁剪，不越界，当前页恒在集合内（期望值由偏移集合推导）。"""
    rels = [f"p{i:02d}.psd" for i in range(10)]
    expected = {rels[index + d] for d in offsets if 0 <= index + d < len(rels)}
    keep = _pages(10, current, offsets)
    assert keep == expected
    assert current in keep, "当前页必须在保留窗口内"
    for rel in keep:
        assert rel in rels, "窗口不得越界到不存在的页"


@pytest.mark.parametrize("offsets", [PRELOAD_WINDOW_OFFSETS_ANDROID,
                                     PRELOAD_WINDOW_OFFSETS_DESKTOP])
def test_window_short_book(offsets):
    """页数少于窗口：全在窗口内，不越界。"""
    assert _pages(3, "p01.psd", offsets) == {"p00.psd", "p01.psd", "p02.psd"}
    assert _pages(1, "p00.psd", offsets) == {"p00.psd"}


def test_window_current_invalid_falls_back_first_page():
    """当前页无效 → 退化到第一页 + 邻域（Android 下即前两页，与桌面不同）。"""
    rels = ["a.psd", "b.psd", "c.psd", "d.psd"]
    android = _window_set(_task(rels, "missing.psd"),
                          offsets=PRELOAD_WINDOW_OFFSETS_ANDROID)
    assert android == {"a.psd", "b.psd"}
    desktop = _window_set(_task(rels, "missing.psd"),
                          offsets=PRELOAD_WINDOW_OFFSETS_DESKTOP)
    assert desktop == set(rels), "桌面：第一页 + 后 3 覆盖全部"
