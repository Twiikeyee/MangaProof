# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""最近打开记录：独立 JSON 文件（程序目录/recent.json）。

需求 §68「最近打开任务」。记录刻意**不**混在 settings.json 里：

- 它是「使用痕迹」，不是「程序设置」——重置设置、设置文件损坏/被整份
  重建、版本升级重建 settings.json，都不该顺手把打开历史清掉；
- settings.json 由设置对话框整体重写，混在里面容易被整份覆盖丢失；
- 写入时机完全不同：每打开一个任务就写一次，与设置变更互不干扰。

文件格式（recent_version 供后续迁移）：

    {
      "recent_version": 1,
      "paths": ["/abs/chapter01", "/abs/001.psd"]
    }

路径一律归一化为绝对路径（`..`/`.`/`~` 展开）后存储，避免同一个文件夹
因写法不同在菜单里出现多条。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from mangaproof.config import paths

log = logging.getLogger("mangaproof.config.recent")

RECENT_VERSION = 1
MAX_RECENT = 10


def normalize(path: str | os.PathLike[str]) -> str:
    """路径归一化：展开 ~、转绝对路径、消除 . 与 ..。

    不要求路径存在——网络盘/移动盘暂时离线时记录必须保留（否则一插拔
    记录就被清空了），因此用 strict=False 的 resolve。
    """
    p = Path(str(path)).expanduser()
    try:
        return str(p.resolve())
    except OSError:          # 极端兜底：个别平台/网络路径解析失败
        return str(p.absolute())


def _same_path(a: str, b: str) -> bool:
    """同一路径判定（Windows 下大小写不敏感）。"""
    return os.path.normcase(a) == os.path.normcase(b)


def _dedupe(items: list[str]) -> list[str]:
    """按 MAX_RECENT 上限去重清洗（保持原有先后顺序）。"""
    result: list[str] = []
    for item in items:
        norm = normalize(item)
        if any(_same_path(norm, kept) for kept in result):
            continue
        result.append(norm)
        if len(result) >= MAX_RECENT:
            break
    return result


class RecentManager:
    """recent.json 的读写封装（最近打开的排最前，最多 MAX_RECENT 条）。"""

    def __init__(
        self,
        path: Path | None = None,
        legacy_paths: list[str] | None = None,
    ):
        self._path = Path(path) if path is not None else paths.recent_paths_path()
        self._lock = threading.Lock()
        self._usable = False          # 文件存在且能解析 → 不再理会旧记录
        self._paths: list[str] = self._load()
        if not self._usable:
            self._migrate_legacy(legacy_paths or [])

    # -- 查询 --------------------------------------------------------------

    @property
    def file_path(self) -> Path:
        """recent.json 完整路径（日志 / 测试用）。"""
        return self._path

    @property
    def paths(self) -> list[str]:
        """当前记录副本（外部改不到内部状态）。"""
        with self._lock:
            return list(self._paths)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._paths

    # -- 变更 --------------------------------------------------------------

    def add(self, path: str | os.PathLike[str]) -> list[str]:
        """记录一次打开：已有则提到最前，超出上限的尾部丢弃，随即落盘。"""
        norm = normalize(path)
        with self._lock:
            self._paths = [p for p in self._paths if not _same_path(p, norm)]
            self._paths.insert(0, norm)
            del self._paths[MAX_RECENT:]
            self._write_locked()
            return list(self._paths)

    def remove(self, path: str | os.PathLike[str]) -> bool:
        """移除一条记录（路径已失效时用）。返回是否真的删掉了。"""
        norm = normalize(path)
        with self._lock:
            kept = [p for p in self._paths if not _same_path(p, norm)]
            if len(kept) == len(self._paths):
                return False
            self._paths = kept
            self._write_locked()
            return True

    def clear(self) -> None:
        """清空全部记录（菜单里的「清除最近打开记录」）。"""
        with self._lock:
            if not self._paths:
                return
            self._paths = []
            self._write_locked()

    # -- 读写 --------------------------------------------------------------

    def _load(self) -> list[str]:
        try:
            if not self._path.exists():
                return []
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("读取最近打开记录失败（%s）：%s", self._path, exc)
            return []

        if not isinstance(raw, dict):
            log.warning("最近打开记录格式异常（期望 JSON 对象）：%s", self._path)
            return []

        self._usable = True
        items = raw.get("paths", [])
        if not isinstance(items, list):
            return []
        return _dedupe([str(p) for p in items if isinstance(p, str) and p.strip()])

    def _migrate_legacy(self, legacy_paths: list[str]) -> None:
        """旧版把最近打开混存在 settings.json：首次启动时搬到 recent.json。

        只在 recent.json 不存在 / 不可解析时执行；旧键会在下一次保存设置时
        从 settings.json 消失（见 SettingsManager.save）。
        """
        cleaned = [str(p) for p in legacy_paths if str(p).strip()]
        if not cleaned:
            return
        with self._lock:
            self._paths = _dedupe(cleaned)
            self._write_locked()
        log.info(
            "已从 settings.json 迁移最近打开记录 %d 条 → %s",
            len(self._paths), self._path,
        )

    def _write_locked(self) -> None:
        """落盘（调用方须持有 self._lock）。原子写：临时文件 + replace。"""
        try:
            payload = {"recent_version": RECENT_VERSION, "paths": self._paths}
            tmp = self._path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except OSError as exc:
            # 程序目录只读等情况下不阻塞使用，仅记录日志
            log.warning("写入最近打开记录失败（%s）：%s", self._path, exc)
