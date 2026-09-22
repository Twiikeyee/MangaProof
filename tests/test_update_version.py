# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""版本解析与比较的单测（需求 §2/§3/§4）。

关键覆盖点：

- ``alpha < beta < stable``（同数字段）；
- 需求 §4 的两个例子（``v1.0.0`` → ``v1.1.0.beta`` 允许；``v1.0.0.beta`` → ``v0.9.0`` 不允许）；
- **无法解析的版本必须抛错**，不能静默判成"无更新"
  （MirrorChyan 的 alpha 通道当前就返回字面量 ``"alpha"``，见调研报告 §3.1）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.update.version import (  # noqa: E402
    AppVersion,
    InvalidVersion,
    channel_of,
    is_newer,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.0.0", "1.0.0"),
        ("v1.0.0", "1.0.0"),
        ("1.1.0.alpha", "1.1.0.alpha"),
        ("v1.1.0.alpha", "1.1.0.alpha"),
        ("1.0.0.beta", "1.0.0.beta"),
        ("  1.2.3  ", "1.2.3"),
    ],
)
def test_parse_and_str(text, expected):
    assert str(AppVersion.parse(text)) == expected


@pytest.mark.parametrize(
    "text", ["", "alpha", "v1", "1.0", "1.0.0.rc1", "1.0.0.stable", "abc"]
)
def test_invalid_versions_raise(text):
    with pytest.raises(InvalidVersion):
        AppVersion.parse(text)
    assert AppVersion.try_parse(text) is None


def test_channel_ordering():
    assert AppVersion.parse("1.1.0.alpha") < AppVersion.parse("1.1.0.beta")
    assert AppVersion.parse("1.1.0.beta") < AppVersion.parse("1.1.0")


def test_numeric_segments_dominate_channel():
    """数字段优先：1.0.0（stable）仍低于 1.0.1.alpha。"""
    assert AppVersion.parse("1.0.0") < AppVersion.parse("1.0.1.alpha")
    assert AppVersion.parse("1.10.0") > AppVersion.parse("1.9.9")


def test_requirement_section4_examples():
    # 当前 v1.0.0，beta 通道 v1.1.0.beta → 允许更新
    assert is_newer("v1.1.0.beta", "v1.0.0") is True
    # 当前 v1.0.0.beta，stable 通道 v0.9.0 → 不允许
    assert is_newer("v0.9.0", "v1.0.0.beta") is False


def test_same_version_is_not_newer():
    assert is_newer("v1.0.0", "1.0.0") is False
    assert is_newer("1.1.0.alpha", "v1.1.0.alpha") is False


def test_beta_to_stable_is_an_update():
    """beta → 同数字段 stable 属于升级（stable 是 beta 之后的正式版）。"""
    assert is_newer("v1.0.0", "v1.0.0.beta") is True


def test_is_newer_raises_on_broken_target():
    """目标版本异常必须抛错——否则会被 UI 误报成"已是最新"。"""
    with pytest.raises(InvalidVersion):
        is_newer("alpha", "1.1.0.alpha")


def test_channel_of():
    assert channel_of("v1.0.0.alpha") == "alpha"
    assert channel_of("1.0.0.beta") == "beta"
    assert channel_of("v1.0.0") == "stable"


def test_current_project_version_is_parseable():
    """仓库当前版本号必须能被自己的解析器吃下（防止格式悄悄漂移）。"""
    from mangaproof import __version__

    parsed = AppVersion.parse(__version__)
    assert str(parsed) == __version__
