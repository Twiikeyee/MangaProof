# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新包校验（需求 §53）。

复用 :mod:`mangaproof.utils.hashing` 的流式哈希实现（零依赖模块），
不另写一份 —— 两处实现分叉是"校验通过但装的包不对"这类事故的温床。
**不要**改成从 ``mangaproof.psd.loader`` 导入：那会把 psd_tools / numpy /
Pillow 拖进安装器（安装器是独立的 onefile 程序，见需求 §43）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from mangaproof.utils.hashing import file_sha256, file_size

log = logging.getLogger("mangaproof.update.checksum")


class ChecksumMismatch(Exception):
    """SHA-256 与期望值不符（需求 §62：必须进入回滚/终止流程）。"""

    def __init__(self, path: Path, expected: str, actual: str):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"SHA-256 校验失败：{path.name}\n期望 {expected}\n实际 {actual}"
        )


def normalize_sha256(value: str) -> str:
    """把各种写法归一成裸小写十六进制。

    - GitHub Release asset 的 ``digest`` 形如 ``sha256:<hex>``（**带前缀**）；
    - R2 的 manifest（需求 §26）存的是**裸 hex**；
    - MirrorChyan 返回的 ``sha256`` 也是裸 hex。
    """
    text = (value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError(f"不是合法的 SHA-256：{value!r}")
    return text


def sha256_of(path: Path) -> str:
    """流式计算文件 SHA-256（大包不占内存）。"""
    return file_sha256(Path(path))


def verify_file(path: Path, expected_sha256: str) -> str:
    """校验文件哈希；不匹配抛 :class:`ChecksumMismatch`，成功返回归一化后的哈希。

    下载流程里"期望值"来源有三条（需求 §19/§22/§24）：MirrorChyan 响应、
    GitHub asset digest、R2 manifest —— 三者入口都先过 :func:`normalize_sha256`。
    """
    expected = normalize_sha256(expected_sha256)
    actual = sha256_of(path)
    if actual != expected:
        raise ChecksumMismatch(Path(path), expected, actual)
    return actual


def verify_size(path: Path, expected_size: int | None) -> bool:
    """大小校验（服务端给了 ``filesize`` 时先做一次廉价检查）。

    返回是否一致；``expected_size`` 为空时返回 ``True``（不参与判定）。
    大小不符通常意味着"下载被截断"，比逐个字节算哈希更早暴露问题。
    """
    if not expected_size:
        return True
    return file_size(Path(path)) == int(expected_size)
