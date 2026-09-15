"""Android 专有界面缩放 +「找回 文件/设置/帮助」的测试。

覆盖：
- **桌面端永远不缩放**（这是本文件最重要的守门测试）：即使 settings.json 里写着
  0.5、即使环境里注入 ANDROID_ROOT，resolve/apply 也必须返回 1.0 且不写
  QT_SCALE_FACTOR —— 记录"为什么不能复用 utils/shutdown.is_android()"；
- Android：默认 75%、设置值覆盖、非法/越界回默认、档位对齐 5%；
- 环境里已有 QT_SCALE_FACTOR 时绝不被覆盖（setdefault 语义）；
- 设置页「界面缩放」仅 Android 出现，21 档 50%…150%，apply_to 能写回；
- 菜单栏属性（AA_DontUseNativeMenuBar）仅 Android 设置；
- 主窗口的 文件/设置/帮助 三个菜单及其动作确实存在。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_android_ui_scale.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

import mangaproof.config.settings as settings_mod
import mangaproof.ui.settings_dialog as sd_mod
from mangaproof.config.settings import (
    ANDROID_DEFAULT_UI_SCALE,
    DEFAULT_UI_SCALE,
    Settings,
    SettingsManager,
    apply_startup_ui_scale,
    clamp_ui_scale,
    effective_ui_scale,
    resolve_ui_scale,
    ui_scale_choices,
)
from mangaproof.utils.platform import is_android_strict


@pytest.fixture(autouse=True)
def _reset_scale_state(monkeypatch):
    """每个用例都从"本次启动还没应用过缩放、环境里没有 QT_SCALE_FACTOR"开始。"""
    settings_mod._effective_ui_scale = None
    monkeypatch.delenv("QT_SCALE_FACTOR", raising=False)
    yield
    settings_mod._effective_ui_scale = None


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _write_raw(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 桌面端守门


def test_is_android_strict_is_env_proof(monkeypatch):
    """严格判定不看环境变量：桌面 Linux 上注入 ANDROID_* 也必须为 False。"""
    if is_android_strict():          # 真机/模拟器上跳过
        pytest.skip("当前运行在 Android 上")
    monkeypatch.setenv("ANDROID_ROOT", "/system")
    monkeypatch.setenv("ANDROID_DATA", "/data")
    monkeypatch.setenv("ANDROID_ASSETS", "/system/app")
    assert is_android_strict() is False


def test_legacy_is_android_would_be_fooled(monkeypatch):
    """反例记录：旧判定会被 ANDROID_ROOT 骗到，所以缩放不能复用它。"""
    from mangaproof.utils.shutdown import is_android

    if is_android_strict():
        pytest.skip("当前运行在 Android 上")
    monkeypatch.setenv("ANDROID_ROOT", "/system")
    assert is_android() is True            # 旧判定：误判
    assert is_android_strict() is False    # 新判定：不受影响


def test_desktop_never_scales(monkeypatch, tmp_path):
    """桌面端：文件里写着 0.5 + 环境里有 ANDROID_ROOT → 仍然 1.0，且不写环境变量。"""
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    monkeypatch.setenv("ANDROID_ROOT", "/system")
    _write_raw(tmp_path, {"ui_scale": 0.5})

    assert resolve_ui_scale(tmp_path) == DEFAULT_UI_SCALE
    assert apply_startup_ui_scale(tmp_path) == DEFAULT_UI_SCALE
    assert "QT_SCALE_FACTOR" not in os.environ
    assert effective_ui_scale() == DEFAULT_UI_SCALE


def test_effective_scale_defaults_to_one_without_apply():
    assert effective_ui_scale() == DEFAULT_UI_SCALE


# ---------------------------------------------------------------- Android 路径


def test_android_default_when_no_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    assert resolve_ui_scale(tmp_path) == ANDROID_DEFAULT_UI_SCALE


def test_android_uses_stored_value_and_writes_env(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    _write_raw(tmp_path, {"ui_scale": 0.9})

    assert resolve_ui_scale(tmp_path) == 0.9
    assert apply_startup_ui_scale(tmp_path) == 0.9
    assert os.environ["QT_SCALE_FACTOR"] == "0.9"
    assert effective_ui_scale() == 0.9


@pytest.mark.parametrize("bad", ["abc", None, 0.1, 3.0, -1, float("nan")])
def test_android_invalid_value_falls_back(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ui_scale": bad}), encoding="utf-8")
    assert resolve_ui_scale(tmp_path) == ANDROID_DEFAULT_UI_SCALE


def test_existing_env_value_is_never_overwritten(monkeypatch, tmp_path):
    """setdefault 语义：用户/系统已设的 QT_SCALE_FACTOR 不被我们覆盖。"""
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    monkeypatch.setenv("QT_SCALE_FACTOR", "1.5")
    _write_raw(tmp_path, {"ui_scale": 0.75})

    assert apply_startup_ui_scale(tmp_path) == 1.5      # 实际生效值 = 环境变量
    assert os.environ["QT_SCALE_FACTOR"] == "1.5"       # 原值保留
    assert effective_ui_scale() == 1.5


def test_ui_scale_choices_are_50_to_150_by_5():
    choices = ui_scale_choices()
    assert len(choices) == 21
    assert choices[0] == 0.5
    assert choices[-1] == 1.5
    assert choices[5] == 0.75          # Android 默认档位在表内
    assert choices[10] == 1.0
    assert all(abs(b - a - 0.05) < 1e-9 for a, b in zip(choices, choices[1:]))


def test_clamp_aligns_to_step_and_rejects_out_of_range():
    assert clamp_ui_scale(0.83) == 0.85
    assert clamp_ui_scale("0.80") == 0.8
    assert clamp_ui_scale(1.0) == 1.0
    assert clamp_ui_scale(0.0) == DEFAULT_UI_SCALE
    assert clamp_ui_scale("abc") == DEFAULT_UI_SCALE


# ---------------------------------------------------------------- 设置持久化


def test_ui_scale_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    path = tmp_path / "settings.json"

    manager = SettingsManager(path)
    assert manager.settings.ui_scale == DEFAULT_UI_SCALE
    manager.settings.ui_scale = 0.8
    manager.save()

    assert json.loads(path.read_text(encoding="utf-8"))["ui_scale"] == 0.8
    assert SettingsManager(path).settings.ui_scale == 0.8


def test_missing_key_uses_platform_default(monkeypatch, tmp_path):
    """有配置文件但没有 ui_scale 键：内存值 = 平台默认（设置页显示即实际生效值）。"""
    _write_raw(tmp_path, {"layer_display_ratio": 0.6})
    path = tmp_path / "settings.json"

    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    assert SettingsManager(path).settings.ui_scale == ANDROID_DEFAULT_UI_SCALE

    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    assert SettingsManager(path).settings.ui_scale == DEFAULT_UI_SCALE


def test_no_settings_file_uses_platform_default(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"      # 不创建文件
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    assert SettingsManager(path).settings.ui_scale == ANDROID_DEFAULT_UI_SCALE


# ---------------------------------------------------------------- 设置对话框


def test_settings_dialog_hides_ui_scale_on_desktop(monkeypatch, qapp):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: False)
    dialog = sd_mod.SettingsDialog(Settings())
    try:
        assert dialog.ui_scale_combo is None
    finally:
        dialog.deleteLater()


def test_settings_dialog_shows_choices_and_writes_back_on_android(monkeypatch, qapp):
    monkeypatch.setattr(settings_mod, "is_android_strict", lambda: True)
    settings = Settings()
    settings.ui_scale = ANDROID_DEFAULT_UI_SCALE

    dialog = sd_mod.SettingsDialog(settings)
    try:
        combo = dialog.ui_scale_combo
        assert combo is not None
        assert combo.count() == 21
        assert combo.currentData() == ANDROID_DEFAULT_UI_SCALE

        combo.setCurrentIndex(combo.findData(1.25))
        out = Settings()
        dialog.apply_to(out)
        assert out.ui_scale == 1.25

        # 恢复默认 → 平台默认（Android 75%），而不是硬编码 1.0
        dialog._reset_defaults()
        assert combo.currentData() == ANDROID_DEFAULT_UI_SCALE
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------- 菜单栏


def test_menu_bar_attribute_only_on_android(monkeypatch, qapp):
    """AA_DontUseNativeMenuBar 只在 Android 设置；桌面一律不碰。"""
    import mangaproof.main as main_mod
    import mangaproof.utils.platform as platform_mod

    calls: list = []

    class _FakeApplication:
        @staticmethod
        def setAttribute(attribute):   # noqa: N802（Qt 命名）
            calls.append(attribute)

    monkeypatch.setattr("PySide6.QtWidgets.QApplication", _FakeApplication)

    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: False)
    assert main_mod.configure_android_menu_bar() is False
    assert calls == []

    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: True)
    assert main_mod.configure_android_menu_bar() is True
    from PySide6.QtCore import Qt

    assert calls == [Qt.ApplicationAttribute.AA_DontUseNativeMenuBar]


def test_main_window_has_file_settings_help_menus(qapp, tmp_path):
    """文件/设置/帮助 三个菜单及其动作必须存在（Android 上可见性靠上面的属性）。"""
    from mangaproof.ui.main_window import MainWindow

    manager = SettingsManager(tmp_path / "settings.json")
    manager.save()
    window = MainWindow(manager)
    try:
        menubar = window.menuBar()
        assert [a.text() for a in menubar.actions()] == ["文件(&F)", "设置(&S)", "帮助(&H)"]

        file_menu = menubar.actions()[0].menu()
        file_actions = [a.text() for a in file_menu.actions()]
        assert "最近打开" in file_actions
        assert "退出" in file_actions

        settings_menu = menubar.actions()[1].menu()
        assert [a.text() for a in settings_menu.actions()] == ["设置…"]

        help_menu = menubar.actions()[2].menu()
        assert [a.text() for a in help_menu.actions()] == ["关于 MangaProof", "第三方许可…"]
    finally:
        window.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
