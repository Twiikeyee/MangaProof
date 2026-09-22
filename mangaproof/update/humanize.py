# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""人类可读的体量/速度格式化（UI 与日志共用）。

需求 §33/§34 要求界面显示"大小：123 MB"与"速度：20.4 MB/s"，
这里统一口径，避免 UI 各处自己拼字符串拼出 1024/1000 两套换算。
"""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(num_bytes: int | float | None) -> str:
    """字节数 → ``"123 MB"``；未知返回 ``"—"``（需求：取不到大小不得阻断流程）。"""
    if num_bytes is None or num_bytes < 0:
        return "—"
    value = float(num_bytes)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"  # pragma: no cover - 理论上到不了


def human_speed(bytes_per_second: float | None) -> str:
    """速度 → ``"20.4 MB/s"``。"""
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/s"
