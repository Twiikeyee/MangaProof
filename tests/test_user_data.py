# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""用户数据白名单单测（需求 §47/§48）。

白名单是"更新时复制哪些东西"的唯一依据，也是安装器的安全边界之一：
路径必须是程序目录内的相对路径，出现 ``..`` 或绝对路径必须在**写入规则时**就失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.config.user_data import (  # noqa: E402
    TYPE_DIRECTORY,
    TYPE_FILE,
    USER_DATA_RULES,
    InvalidRule,
    absolute_targets,
    relative_paths,
    rule_for,
    validate_rules,
)


def test_current_whitelist_matches_requirement():
    """需求 §47 规定当前白名单就是这三项。"""
    assert relative_paths() == ("settings.json", "recent.json", "logs")
    assert rule_for("logs")["type"] == TYPE_DIRECTORY
    assert rule_for("settings.json")["type"] == TYPE_FILE


def test_whitelist_is_valid():
    validate_rules()


def test_whitelist_paths_exist_in_the_app_dir_convention():
    """白名单路径必须与 config/paths.py 的实际落点一致（否则备份会漏文件）。"""
    from mangaproof.config import paths

    app_dir = paths.get_app_dir()
    assert paths.settings_path() == app_dir / "settings.json"
    assert paths.recent_paths_path() == app_dir / "recent.json"
    assert paths.logs_dir() == app_dir / "logs"
    for rel in relative_paths():
        assert (app_dir / rel) in (
            paths.settings_path(),
            paths.recent_paths_path(),
            paths.logs_dir(),
        ), rel


def test_rule_for_unknown_path():
    assert rule_for("workspace.json") is None
    assert rule_for("../evil") is None


@pytest.mark.parametrize(
    "rules",
    [
        ({"path": "/etc/passwd", "type": "file"},),                 # 绝对路径
        ({"path": "../outside", "type": "file"},),                 # 路径穿越
        ({"path": "logs/../../x", "type": "file"},),               # 穿越（带合法前缀）
        ({"path": "settings.json", "type": "symlink"},),           # 未知类型
        ({"path": "", "type": "file"},),                           # 空路径
        ({"path": "a", "type": "file"}, {"path": "a", "type": "file"}),  # 重复
    ],
)
def test_invalid_rules_are_rejected(rules):
    with pytest.raises(InvalidRule):
        validate_rules(rules)


def test_absolute_targets_resolves_against_base(tmp_path: Path):
    targets = absolute_targets(tmp_path, USER_DATA_RULES)
    assert targets[0] == (tmp_path / "settings.json", TYPE_FILE)
    assert targets[2] == (tmp_path / "logs", TYPE_DIRECTORY)


def test_default_rules_are_used_when_omitted(tmp_path: Path):
    assert len(absolute_targets(tmp_path)) == len(USER_DATA_RULES)
