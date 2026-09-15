"""字体字形覆盖守门测试：界面上出现的每个字符，MiSans 必须都有字形。

为什么需要（Android 专有坑）：Android 版 Qt 的字体数据库**几乎不提供回退**——
`QAndroidPlatformFontDatabase::fallbacksForFamily()` 只追加 emoji 字体、按系统语言
追加一个 CJK 字体，以及 `QT_ANDROID_FONTS` 里列出的家族名；它**不会**把
`/system/fonts` 下的字体当作逐字回退【源码】qtbase/src/plugins/platforms/android/
qandroidplatformfontdatabase.cpp。于是：MiSans 缺哪个字形，Android 上就显示成空白
（本应用 0.75 缩放的平板上实测：✗ ▣ ✎ 🗑 ⚠ 都是空白），而桌面端靠系统字体回退
（Windows DirectWrite / macOS CoreText / Linux fontconfig）看不出来。

本测试直接解析 MiSans 的 cmap（复用 reportlab 的 TTFontFile——与返修单 PDF 同一套
字体解析），扫描 `mangaproof/` 下所有**非 docstring** 的字符串字面量（= 界面文案与
日志文案），断言零缺字。docstring 不参与显示，故跳过。

运行：uv run python -m pytest tests/test_font_glyph_coverage.py -v
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from reportlab.pdfbase.ttfonts import TTFontFile

sys.path.insert(0, str(Path(__file__).parent.parent))

from mangaproof.fonts import find_font_path

PACKAGE_DIR = Path(__file__).parent.parent / "mangaproof"

#: 曾经用过、但 MiSans 没有字形（Android 上会空白）的历史字符。
#: 保留这份清单是为了让"回归"时的失败信息更直白。
LEGACY_MISSING_GLYPHS = {
    "✗": "U+2717 BALLOT X（改用 ✕ U+2715）",
    "▣": "U+25A3 WHITE SQUARE CONTAINING BLACK SMALL SQUARE（改用 □ U+25A1）",
    "✎": "U+270E LOWER RIGHT PENCIL（MiSans 无铅笔类字形，改为纯文字）",
    "🗑": "U+1F5D1 WASTEBASKET（改为纯文字）",
    "⚠": "U+26A0 WARNING SIGN（改用 ▲ U+25B2）",
}


def _misans_codepoints() -> set[int]:
    """MiSans 实际覆盖的码位集合（不存在字体文件时跳过）。"""
    path = find_font_path()
    if path is None:
        pytest.skip("未找到 MiSans 字体文件，跳过字形覆盖检查")
    face = TTFontFile(str(path))
    return {int(cp) for cp in face.charToGlyph}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """所有 docstring 字面量节点的 id()（这些不参与界面显示，跳过）。"""
    keys = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, keys) or not getattr(node, "body", None):
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            ids.add(id(first.value))
    return ids


def _displayed_characters() -> dict[str, list[str]]:
    """`mangaproof/` 下非 docstring 字符串字面量里出现的非 ASCII 字符 → 位置列表。"""
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            for ch in node.value:
                if ord(ch) < 128:
                    continue
                found.setdefault(ch, []).append(f"{path.name}:{node.lineno}")
    return found


def test_misans_covers_every_displayed_character():
    """界面/日志里出现的每个非 ASCII 字符都必须有 MiSans 字形。"""
    covered = _misans_codepoints()
    missing = {ch: locs for ch, locs in _displayed_characters().items() if ord(ch) not in covered}
    if missing:
        lines = [
            f"  {ch}  U+{ord(ch):04X}  出现于 {', '.join(locs[:4])}"
            + (f"  ← 建议：{LEGACY_MISSING_GLYPHS[ch]}" if ch in LEGACY_MISSING_GLYPHS else "")
            for ch, locs in sorted(missing.items(), key=lambda kv: ord(kv[0]))
        ]
        pytest.fail(
            "以下字符 MiSans 没有字形，Android 上会显示成空白方块"
            "（Qt for Android 不做逐字回退），请改用有字形的等价字符：\n" + "\n".join(lines)
        )


@pytest.mark.parametrize("ch", sorted(LEGACY_MISSING_GLYPHS))
def test_legacy_missing_glyphs_are_gone(ch: str):
    """历史缺字字符不得再出现在任何非 docstring 字面量里。"""
    assert ch not in _displayed_characters(), LEGACY_MISSING_GLYPHS[ch]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
