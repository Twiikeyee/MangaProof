# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""版本号解析与比较（需求 §3、§4）。

版本格式（需求 §2）：

.. code-block:: text

    alpha  → v1.0.0.alpha
    beta   → v1.0.0.beta
    stable → v1.0.0

比较语义（需求 §3/§4）：

- 是否更新**只看** ``目标版本 > 当前版本``；
- MirrorChyan 的 ``version_number`` 是**服务端资源上传计数**，
  **绝不参与**版本比较（§3）；
- 同一数字段下 ``alpha < beta < stable``：
  例如当前 ``v1.0.0.beta``、目标 ``v1.0.0`` → 允许更新（stable 是 beta 之后的正式版）；
  当前 ``v1.0.0``、目标 ``v1.0.0.alpha`` → 不更新。

为什么要自己写而不是用 :mod:`packaging`：``packaging`` 是开发/传递依赖，
运行期引入等于隐式依赖（见 AI-rules 的依赖规则），
且本模块只需要"数字段 + 通道序"这一条明确规则，自写更可控、更好测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 允许 ``v`` 前缀；数字必须是三段；通道后缀只能是 alpha / beta（stable 无后缀）
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\.(alpha|beta))?$")

CHANNEL_ALPHA = "alpha"
CHANNEL_BETA = "beta"
CHANNEL_STABLE = "stable"

#: 更新分支取值（需求 §2），顺序即 UI 下拉顺序
CHANNELS: tuple[str, ...] = (CHANNEL_STABLE, CHANNEL_BETA, CHANNEL_ALPHA)

#: 通道优先级：数字段相同时，rank 大者更新（alpha 0 < beta 1 < stable 2）
_CHANNEL_RANK: dict[str, int] = {
    CHANNEL_ALPHA: 0,
    CHANNEL_BETA: 1,
    CHANNEL_STABLE: 2,
}
_RANK_CHANNEL: dict[int, str] = {v: k for k, v in _CHANNEL_RANK.items()}


class InvalidVersion(ValueError):
    """版本号无法解析（例如 MirrorChyan 上被错传成字面量 "alpha" 的资源）。"""


@dataclass(frozen=True, order=True)
class AppVersion:
    """可比较的软件版本。

    ``order=True`` 生成的比较按字段声明顺序（major → minor → patch → channel_rank），
    正是需求要求的顺序，无需另写 ``__lt__``。
    """

    major: int
    minor: int
    patch: int
    channel_rank: int = _CHANNEL_RANK[CHANNEL_STABLE]

    @classmethod
    def parse(cls, text: str) -> "AppVersion":
        """解析版本串（允许 ``v`` 前缀）。失败抛 :class:`InvalidVersion`。"""
        raw = (text or "").strip()
        m = _VERSION_RE.match(raw)
        if not m:
            raise InvalidVersion(f"无法解析版本号：{text!r}")
        major, minor, patch, channel = m.groups()
        return cls(
            int(major), int(minor), int(patch),
            _CHANNEL_RANK[channel or CHANNEL_STABLE],
        )

    @classmethod
    def try_parse(cls, text: str) -> "AppVersion | None":
        """宽松解析：失败返回 ``None``（供"通道版本信息异常"这类分支使用）。"""
        try:
            return cls.parse(text)
        except InvalidVersion:
            return None

    @property
    def channel(self) -> str:
        """稳定通道名（``alpha`` / ``beta`` / ``stable``）。"""
        return _RANK_CHANNEL[self.channel_rank]

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.channel == CHANNEL_STABLE else f"{base}.{self.channel}"

    def display(self) -> str:
        """带 ``v`` 前缀的展示形式（与 GitHub tag / MirrorChyan 一致）。"""
        return f"v{self}"


def channel_of(text: str) -> str:
    """从版本串推断更新分支（需求 §70：``v1.0.0.alpha → alpha``）。"""
    return AppVersion.parse(text).channel


def is_newer(target: str | AppVersion, current: str | AppVersion) -> bool:
    """``目标版本 > 当前版本``（需求 §3/§4 的唯一判定）。

    任一版本无法解析时抛 :class:`InvalidVersion` —— 由调用方决定怎么提示，
    绝不静默返回 ``False``（那会把"版本信息异常"伪装成"已是最新"）。
    """
    t = target if isinstance(target, AppVersion) else AppVersion.parse(target)
    c = current if isinstance(current, AppVersion) else AppVersion.parse(current)
    return t > c
