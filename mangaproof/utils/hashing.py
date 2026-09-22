# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""文件哈希/大小（纯标准库）。

为什么单独成一个模块：这两个函数有两类调用方——

- 主程序（``psd/loader.py`` → ``review/persistence.py`` 的断点续跑身份校验）；
- **自更新安装器**（``mangaproof/update/checksum.py``，需求 §53 的包校验）。

安装器是 PyInstaller onefile 构建的独立程序（需求 §43「必须完全独立，不依赖主程序
运行环境」）。如果它经由 ``mangaproof.psd.loader`` 拿哈希，就会把 ``psd_tools`` /
numpy / Pillow 一并拖进安装器，体积和启动时间都不可接受。因此把实现放在这个
**零依赖**模块里，两边共用同一份代码（避免"两处实现对不上"导致的校验事故）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: 流式读取块大小（1 MiB：大包下载校验时兼顾速度与内存）
CHUNK_SIZE = 1024 * 1024


def file_size(path: Path) -> int:
    """文件字节数。"""
    return Path(path).stat().st_size


def file_sha256(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """流式计算完整 SHA-256（避免一次性读入内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
