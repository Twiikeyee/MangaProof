# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""用户数据白名单（需求 §47、§48）。

更新时"哪些东西算用户数据、需要备份并在新版里恢复"由**源码里的这张表**决定：

.. code-block:: python

    USER_DATA_RULES = (
        {"path": "settings.json", "type": "file"},
        {"path": "recent.json", "type": "file"},
        {"path": "logs", "type": "directory"},
    )

维护原则（需求 §48）：

- 白名单**本身是软件逻辑**，不属于用户数据，因此写死在源码里，
  **不引入** ``user_data_whitelist.json`` 之类的外部清单；
- 以后新增需要迁移的用户数据，只在这里加一条规则，安装器无需改动。

范围约束（重要）：

- ``path`` 一律是**相对程序目录**的路径，不接受绝对路径或 ``..``
  （安装器会把它拼到 ``--install-dir`` 上，见需求 §52 的路径穿越防护）；
- 本模块**只描述数据**，不做复制/恢复（那是 ``updater/backup.py`` 的职责）；
- 本模块必须保持**零依赖**（只用标准库）：安装器是独立的 onefile 程序
  （需求 §43），它导入本模块时不能顺带拖进 PySide6 等主程序依赖。
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

TYPE_FILE = "file"
TYPE_DIRECTORY = "directory"

#: 用户数据白名单（需求 §47 的当前清单）
USER_DATA_RULES: tuple[dict[str, str], ...] = (
    {"path": "settings.json", "type": TYPE_FILE},
    {"path": "recent.json", "type": TYPE_FILE},
    {"path": "logs", "type": TYPE_DIRECTORY},
)

_VALID_TYPES = frozenset({TYPE_FILE, TYPE_DIRECTORY})


class InvalidRule(ValueError):
    """白名单规则写错了（路径穿越、绝对路径、未知类型等）。"""


def validate_rules(rules=USER_DATA_RULES) -> None:
    """自检白名单（由单测与安装器启动时调用）。

    非法即抛 :class:`InvalidRule`：白名单是安全边界的一部分，
    写错必须**早失败**，不能等到更新时才带着 ``../`` 去复制文件。
    """
    seen: set[str] = set()
    for rule in rules:
        rel = str(rule.get("path", ""))
        kind = str(rule.get("type", ""))
        if kind not in _VALID_TYPES:
            raise InvalidRule(f"未知的条目类型：{kind!r}（规则：{rule!r}）")
        if not rel or rel in seen:
            raise InvalidRule(f"路径为空或重复：{rel!r}")
        pure = PurePosixPath(rel.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or len(pure.parts) != len(
            [p for p in pure.parts if p not in ("", ".")]
        ):
            raise InvalidRule(f"路径必须是程序目录内的相对路径：{rel!r}")
        seen.add(rel)


def relative_paths(rules=USER_DATA_RULES) -> tuple[str, ...]:
    """白名单里的相对路径（备份/恢复的遍历顺序即此顺序）。"""
    return tuple(str(r["path"]) for r in rules)


def rule_for(rel_path: str, rules=USER_DATA_RULES) -> dict[str, str] | None:
    """按相对路径取规则；不在白名单内返回 ``None``。"""
    target = PurePosixPath(str(rel_path).replace("\\", "/")).as_posix()
    for rule in rules:
        if PurePosixPath(str(rule["path"])).as_posix() == target:
            return dict(rule)
    return None


def absolute_targets(base_dir: Path, rules=USER_DATA_RULES) -> list[tuple[Path, str]]:
    """把白名单解析成 ``[(绝对路径, 类型), …]``。

    ``base_dir`` 由调用方给出（主程序传程序目录，安装器传 ``--install-dir``），
    本模块不自己判断"程序目录在哪"——那是 ``config/paths.py`` 与平台模块的事。
    """
    validate_rules(rules)
    base = Path(base_dir)
    return [(base / str(r["path"]), str(r["type"])) for r in rules]


__all__ = [
    "USER_DATA_RULES",
    "TYPE_FILE",
    "TYPE_DIRECTORY",
    "InvalidRule",
    "validate_rules",
    "relative_paths",
    "rule_for",
    "absolute_targets",
]
