"""Android 浮动方向键（NavPad）：创建条件、置灰规则、直连动作、定位。

对应需求方的三条决策：**B**（Viewer 右下角浮动）+ **①**（直接调用动作方法，
不伪造按键）+ **置灰**（到边界/未打开任务时禁用，不做长按连发）。

桌面端必须**完全不创建**该控件 —— 这是"桌面零改动"的可验证形式。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from mangaproof.config.settings import SettingsManager
from mangaproof.ui.main_window import MainWindow

DATA_DIR = Path(__file__).parent / "data" / "chapter01"

app = QApplication.instance() or QApplication([])


@pytest.fixture()
def window(tmp_path):
    w = MainWindow(SettingsManager(tmp_path / "settings.json"))
    yield w
    w.close()
    app.processEvents()


@pytest.fixture()
def android(monkeypatch):
    """把平台判定切成"在 Android 上"。

    创建与否由 MainWindow._setup_nav_pad 判定，所以只需要改 main_window 里的引用
    （NavPad 自身不做平台判定，见其模块文档）。
    """
    import mangaproof.ui.main_window as mw
    monkeypatch.setattr(mw, "is_android_strict", lambda: True)


def _wait(predicate, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待超时")


def _open_task(window: MainWindow, tmp_path: Path) -> None:
    folder = tmp_path / "chapter"
    folder.mkdir(exist_ok=True)
    for psd in sorted(DATA_DIR.glob("*.psd"))[:2]:
        shutil.copy2(psd, folder / psd.name)
    window.open_folder(folder)
    _wait(lambda: window.task is not None and window._current_file != "")


# --------------------------------------------------------------------- 创建条件

def test_desktop_does_not_create_nav_pad(window):
    """桌面端不得创建该控件（布局零改动的可验证形式）。"""
    assert window.nav_pad is None


def test_android_creates_four_buttons(android, window):
    pad = window.nav_pad
    assert pad is not None
    for btn in (pad.btn_up, pad.btn_down, pad.btn_left, pad.btn_right):
        assert btn.text() in ("▲", "▼", "◀", "▶")
        assert btn.focusPolicy().name == "NoFocus"      # 点击不抢焦点
    assert pad.parent() is window.viewer                # 浮动在画布上


def test_hidden_until_task_opened(android, window):
    """未打开任务：四个禁用且整体隐藏（空画布上不该出现一组不能用的按钮）。"""
    pad = window.nav_pad
    assert pad is not None
    # 用 isHidden() 而不是 isVisible()：无头测试里父窗口从未 show()，
    # 子控件的 isVisible() 恒为 False，判不出"我们主动隐藏了它"。
    assert pad.isHidden()
    assert not any(b.isEnabled() for b in (pad.btn_up, pad.btn_down, pad.btn_left, pad.btn_right))


# --------------------------------------------------------------------- 置灰规则

def test_edge_states_after_open(android, window, tmp_path):
    """第一个 PSD 的第一个图层：▲ 与 ◀ 必须置灰；内容存在时 ▼/▶ 可用。"""
    _open_task(window, tmp_path)
    pad = window.nav_pad
    assert not pad.isHidden()
    assert not pad.btn_up.isEnabled()          # 已在第一个 PSD
    assert not pad.btn_left.isEnabled()        # 已在第一个图层
    assert pad.btn_down.isEnabled()            # 后面还有 PSD
    assert pad.btn_right.isEnabled()           # 后面还有图层


def test_buttons_track_position(android, window, tmp_path):
    """走到末尾：▼ 与 ▶ 也置灰（置灰而不是"点了没反应"）。"""
    _open_task(window, tmp_path)
    pad = window.nav_pad
    window.next_psd()
    # 等"异步打开真正落定"：_current_file 是在打开完成前就赋值的（_switch_file 是
    # 异步的），所以判据要用"末尾状态已刷新到按钮上"，否则会抢跑。
    _wait(lambda: window._current_file != "001.psd" and not pad.btn_down.isEnabled())
    assert not pad.btn_down.isEnabled()        # 只有 2 个 PSD，已在最后一个
    assert pad.btn_up.isEnabled()
    while pad.btn_right.isEnabled():           # 一直往右直到最后一个图层
        window.next_layer()
        app.processEvents()
    assert not pad.btn_right.isEnabled()
    assert pad.btn_left.isEnabled()


def test_disabled_after_close_task(android, window, tmp_path):
    _open_task(window, tmp_path)
    window.close_task()
    app.processEvents()
    pad = window.nav_pad
    assert pad.isHidden()
    assert not any(b.isEnabled() for b in (pad.btn_up, pad.btn_down, pad.btn_left, pad.btn_right))


# --------------------------------------------------------------------- 直连动作（①）

def test_buttons_are_wired_to_the_four_actions(android, window, tmp_path):
    """四个按钮必须**直接**指向四个动作方法（决策 ①：不伪造按键事件）。

    注意不能"先建窗口再 patch 实例方法"来断言 —— 回调是在创建时就绑定的，
    事后替换实例属性不会影响已建立的绑定（这里踩过）。所以直接比对绑定来源：
    回调背后必须就是 MainWindow 的那四个方法本身。
    """
    _open_task(window, tmp_path)
    pad = window.nav_pad
    expected = {
        "prev_file": window.prev_psd,
        "next_file": window.next_psd,
        "prev_layer": window.prev_layer,
        "next_layer": window.next_layer,
    }
    assert set(pad._callbacks) == set(expected)
    for key, method in expected.items():
        assert pad._callbacks[key] == method, key          # 同源即可（bound method 可比）


def test_click_actually_navigates(android, window, tmp_path):
    """真实点击要真的产生导航效果（不只是调了个桩）。"""
    _open_task(window, tmp_path)
    pad = window.nav_pad
    first = window._current_file
    pad.btn_down.click()
    _wait(lambda: window._current_file != first)
    assert window._current_file != first


# --------------------------------------------------------------------- 定位

def test_pad_kept_inside_bottom_right(android, window, tmp_path):
    """贴在画布右下角，且随画布尺寸变化重新定位。"""
    _open_task(window, tmp_path)
    pad = window.nav_pad
    window.resize(1200, 800)
    app.processEvents()
    pad.reposition()
    viewer = window.viewer
    assert pad.x() + pad.width() <= viewer.width()
    assert pad.y() + pad.height() <= viewer.height()
    assert pad.x() > viewer.width() / 2          # 右半边
    assert pad.y() > viewer.height() / 2         # 下半边
