"""字体回退链测试（Android 缺字形的修复机制）。

背景：Android 版 Qt 的平台字体回退几乎是空的
（`QAndroidPlatformFontDatabase::fallbacksForFamily()` 只加 emoji / 按语言的 CJK /
`QT_ANDROID_FONTS` 名单），而 MiSans 缺若干符号字形（✗ U+2717、⚠ U+26A0 等），
于是平板上显示成空白。Qt 的逐字回退候选其实来自**字体家族链**：
`QFont.setFamilies([A, B, C])` → `fallBackFamilies = [B, C]` → `QFontEngineMulti`
按链查找【源码】qfontdatabase.cpp:2847-2876、810-827。

本测试守住这条链：
- 回退字体（font/JetBrainsMonoNerdFont-Regular-v1.2.ttf）能找到、能注册、家族名正确；
- **只在 Android** 挂进链（桌面返回空，观感不变）；
- 主题的 QSS 家族链顺序为 MiSans → 回退字体 → 桌面默认家族；
- MiSans 确实没有 ✗/⚠ 字形（说明为什么需要回退）；
- 关键行为：链 `[MiSans, 回退字体]` 渲染 ✗/⚠ 的结果，与单独用回退字体渲染**逐像素相同**
  ⇒ 字形确实来自回退字体（这正是 Android 上补字形所依赖的机制）。

运行：QT_QPA_PLATFORM=offscreen uv run python -m pytest tests/test_font_fallback.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication, QLabel

from mangaproof.fonts import (
    fallback_font_candidates,
    find_font_path,
    load_symbol_fallback_families,
)
from mangaproof.ui.theme import DEFAULT_FONT_FAMILIES, apply_dark_theme

FALLBACK_FAMILY = "JetBrainsMono Nerd Font"
#: 回退字体确实提供、MiSans 确实缺失的字形（本轮修复依赖这两个）
FALLBACK_COVERED = ("✗", "⚠")
#: 这两个字体都没有、Android 上仍会空白的字形（▣ 自动框选 / ✎ 自定义批注 / 🗑 删除）
STILL_UNCOVERED = ("▣", "✎", "🗑")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def fonts(qapp):
    """注册 MiSans 与回退字体，返回 (MiSans 家族名, 回退家族名)。"""
    misans_path = find_font_path()
    if misans_path is None:
        pytest.skip("未找到 MiSans 字体")
    fb_path = next((p for p in fallback_font_candidates() if p.exists()), None)
    if fb_path is None:
        pytest.skip("未找到回退字体")

    def _family(path: Path) -> str:
        font_id = QFontDatabase.addApplicationFont(str(path))
        assert font_id >= 0, f"字体注册失败：{path}"
        families = QFontDatabase.applicationFontFamilies(font_id)
        assert families, f"字体没有可用家族名：{path}"
        return families[0]

    return _family(misans_path), _family(fb_path)


def _render(ch: str, families: list[str], size: int = 64) -> bytes:
    """把单个字符渲染到 QImage，返回像素字节（用于逐像素比对字形来源）。"""
    font = QFont()
    font.setPointSize(15)
    font.setFamilies(families)
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setFont(font)
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, ch)
    painter.end()
    return bytes(image.constBits())


def test_fallback_font_family_name(fonts):
    assert fonts[1] == FALLBACK_FAMILY


def test_fallback_families_are_android_only(monkeypatch, fonts):
    """桌面返回空（不改变桌面观感）；Android 才返回回退家族。"""
    import mangaproof.utils.platform as platform_mod

    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: False)
    assert load_symbol_fallback_families() == []

    monkeypatch.setattr(platform_mod, "is_android_strict", lambda: True)
    assert load_symbol_fallback_families() == [FALLBACK_FAMILY]


def test_theme_family_chain_order(qapp, fonts):
    """QSS 家族链顺序：MiSans → 回退字体 → 桌面默认家族。"""
    misans, fallback = fonts
    apply_dark_theme(qapp, primary_family=misans, fallback_families=[fallback])
    label = QLabel("✗ 未通过")
    label.show()
    qapp.processEvents()
    expected = [misans, fallback, *DEFAULT_FONT_FAMILIES]
    assert label.font().families() == expected
    label.close()


def test_desktop_chain_unchanged_without_fallback(qapp, fonts):
    """不传回退家族时，链与改动前一致（桌面路径）。"""
    misans, _ = fonts
    apply_dark_theme(qapp, primary_family=misans)
    label = QLabel("测试")
    label.show()
    qapp.processEvents()
    assert label.font().families() == [misans, *DEFAULT_FONT_FAMILIES]
    label.close()


def test_misans_lacks_the_fallback_glyphs(fonts):
    """MiSans 确实没有 ✗/⚠ 字形（否则不需要回退）。"""
    from reportlab.pdfbase.ttfonts import TTFontFile

    misans_path = find_font_path()
    assert misans_path is not None
    cmap = TTFontFile(str(misans_path)).charToGlyph
    for ch in FALLBACK_COVERED:
        assert ord(ch) not in cmap, f"MiSans 竟然有 {ch}（U+{ord(ch):04X}）字形"


def test_glyphs_come_from_fallback_font(fonts):
    """核心断言：链 [MiSans, 回退字体] 渲染 ✗/⚠ 与单独用回退字体渲染逐像素相同，
    且该渲染**非空白** ⇒ 字形确实来自家族链里的回退字体。

    这正是 Android 上能补出字形的机制——字形来自家族链，而不是依赖平台字体回退
    （Android 上那条路是空的；桌面本机有平台回退，所以要额外断言非空白，
    以免"两边都是空白"造成假阳性）。
    """
    misans, fallback = fonts
    blank = _render("\uE000", [fallback])   # 私用区：回退字体也没有 → 空白基准
    for ch in FALLBACK_COVERED:
        from_fallback_alone = _render(ch, [fallback])
        through_chain = _render(ch, [misans, fallback])
        assert from_fallback_alone != blank, f"{ch} 在回退字体里是空白（覆盖结论有误）"
        assert through_chain == from_fallback_alone, f"{ch} 未从回退字体取字形"


def test_known_uncovered_glyphs_are_still_missing(fonts):
    """记录当前缺口：▣/✎/🗑 两个字体都没有（Android 上仍会空白，待后续决定）。

    这个测试不是"要求它们缺失"，而是把现状钉住：一旦将来补了覆盖它们的字体，
    这里会失败并提醒同步更新按钮文案/文档。
    """
    from reportlab.pdfbase.ttfonts import TTFontFile

    fb_path = next((p for p in fallback_font_candidates() if p.exists()), None)
    assert fb_path is not None
    cmap = TTFontFile(str(fb_path)).charToGlyph
    for ch in STILL_UNCOVERED[:2]:   # ▣ ✎ 是 BMP，reportlab 可判定
        assert ord(ch) not in cmap


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
