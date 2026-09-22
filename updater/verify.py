# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新前最终校验（需求 §53）。

在任何**破坏性操作**（``MangaProof → MangaProof.old``、解压、恢复数据）之前
必须全部完成：

.. code-block:: text

    更新包存在
    +
    SHA-256 正确
    +
    压缩结构正确
    +
    主程序存在

只要有一条不成立就**不得**进入替换流程（§62：SHA-256 错误 / 压缩包损坏 /
结构错误都在失败清单里）。因此本模块的函数全是只读的：调用它们不会改动
``--install-dir``（单测里会显式断言这一点）。

复用（不重复实现）：

- :func:`mangaproof.update.checksum.verify_file`（内部走 ``utils.hashing``）；
- :mod:`updater.archive` 的格式/结构校验。
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mangaproof.update.checksum import ChecksumMismatch, verify_file
from mangaproof.utils.hashing import file_size

from updater import archive

log = logging.getLogger("mangaproof.updater.verify")

ItemCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]

#: 校验步骤总数（UI 的 "n/4"；需求 §53 的四件事）
VERIFY_STEPS = 4


class VerifyError(Exception):
    """校验失败（需求 §62：进入失败/回滚流程，不再继续）。"""


@dataclass(frozen=True)
class PackageVerification:
    """校验通过的更新包信息（供日志与状态文件记录）。"""

    package: Path
    sha256: str
    size: int
    info: archive.ArchiveInfo

    @property
    def hash_checked(self) -> bool:
        return bool(self.sha256)


def _step(
    on_item: ItemCallback | None,
    on_progress: ProgressCallback | None,
    index: int,
    rel: str,
) -> None:
    if on_item is not None:
        on_item(rel)
    if on_progress is not None:
        on_progress(index, VERIFY_STEPS)


def verify_package(
    package: Path,
    *,
    platform: str,
    expected_sha256: str = "",
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> PackageVerification:
    """§53 的四步校验；失败抛 :class:`VerifyError`（**只读，不碰安装目录**）。"""
    path = Path(package)

    # 1) 更新包存在
    if not path.is_file():
        raise VerifyError(f"更新包不存在：{path}（需求 §53）")
    size = file_size(path)
    if size <= 0:
        raise VerifyError(f"更新包是空文件：{path}（需求 §53）")
    _step(on_item, on_progress, 1, path.name)

    # 2) SHA-256 正确（--sha256 允许为空字符串：此时只能跳过并留痕）
    actual = ""
    if expected_sha256:
        try:
            actual = verify_file(path, expected_sha256)
        except ChecksumMismatch as exc:
            raise VerifyError(f"{exc}（需求 §62：SHA-256 错误必须终止更新）") from exc
        except ValueError as exc:
            raise VerifyError(f"期望的 SHA-256 不合法：{exc}（需求 §45）") from exc
    else:
        log.warning("未提供 --sha256，跳过哈希校验：%s（需求 §53 建议由主程序传入）", path.name)
        actual = ""
    _step(on_item, on_progress, 2, f"{path.name}（SHA-256）")

    # 3) 压缩结构正确 + 4) 主程序存在（archive.inspect_package 一次读包完成两件事）
    try:
        info = archive.inspect_package(path, platform)
    except archive.ArchiveError as exc:
        raise VerifyError(f"{exc}（需求 §53/§62）") from exc
    _step(on_item, on_progress, 3, f"{path.name}（压缩结构）")
    _step(on_item, on_progress, 4, info.main_rel)

    return PackageVerification(package=path, sha256=actual, size=size, info=info)


def verify_installed(
    install_dir: Path,
    platform: str,
    *,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """解压完成后断言"主程序就位 + 可执行"（§52/§53/§57）。

    返回主程序路径；缺失即抛 :class:`VerifyError`（此时**必须**回滚，§62）。
    """
    root = Path(install_dir)
    rel = archive.install_main_rel(platform)
    if not root.exists():
        raise VerifyError(f"安装目录不存在：{root}（需求 §53）")
    entry = archive.find_main_executable(root, platform)
    _step(on_item, on_progress, 1, rel)
    if entry is None:
        raise VerifyError(f"新版本缺少主程序：{root / rel}（需求 §52/§57）")
    if platform in ("linux", "macos"):
        try:
            mode = entry.stat().st_mode
        except OSError as exc:
            raise VerifyError(f"主程序无法访问：{entry}（{exc}）") from exc
        if not mode & stat.S_IXUSR:
            # 需求 §41：启动前确认可执行，必要时 chmod +x（不因权限位缺失直接判失败）
            try:
                os.chmod(entry, (mode | 0o755) & 0o777)
                log.warning("主程序缺少可执行位，已 chmod +x：%s", entry)
            except OSError as exc:
                raise VerifyError(f"主程序不可执行且 chmod 失败：{entry}（{exc}）") from exc
    return entry


__all__ = [
    "VERIFY_STEPS",
    "PackageVerification",
    "VerifyError",
    "verify_installed",
    "verify_package",
]
