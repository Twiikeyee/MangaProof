# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""平台/架构识别与 MirrorChyan 映射（需求 §9，7 组 endpoint 见 §11.5 规格）。

本模块是**纯标准库**的：只用 :mod:`sys` / :mod:`platform` / :func:`is_android_strict`，
不导入任何平台专有第三方库，也不做网络请求，因此可以随便单测。

判定的两条硬规则：

1. **架构取值只认规格表里的字面量**（``x64`` / ``arm64`` / ``aarch64`` / ``x86_64``）。
   MirrorChyan 的响应会把它们归一化后回显（``x64→amd64``、``macos→darwin``、
   ``aarch64→arm64``），**回显值不得用于构造请求**。
2. **Android 判定必须用** :func:`mangaproof.utils.platform.is_android_strict`
   （编译期事实，不看环境变量）。p4a 的 Python 上报 ``sys.platform == "linux"``，
   所以 Android 分支必须排在 Linux 之前，否则会把 APK 当成 Linux 桌面版。
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass

from mangaproof.utils.platform import is_android_strict

OS_WINDOWS = "windows"
OS_LINUX = "linux"
OS_MACOS = "macos"
OS_ANDROID = "android"

#: MirrorChyan 资源标识（rid）：桌面端与 Android 端是**两个**资源（需求 §18（18/65））
RID_DESKTOP = "MangaProof"
RID_ANDROID = "MangaProof_exec"

#: MirrorChyan 下载信息请求必须带的 user_agent（需求 §20）
USER_AGENT_DESKTOP = "MangaProofUpgrade"
USER_AGENT_ANDROID = "MangaProof_exec"

MIRRORCHYAN_BASE = "https://mirrorchyan.com/api/resources"

#: Release 资产名（与 .github/workflows 的产物命名一致，需求 §22）
ARTIFACT_PREFIX = "MangaProof"


@dataclass(frozen=True)
class Target:
    """一个受支持的"平台 + 架构"组合。"""

    os_key: str        # windows / linux / macos / android —— 请求参数与资产名同一取值
    arch_key: str      # x64 / arm64 / aarch64 / x86_64 —— 同上
    rid: str           # MirrorChyan 资源标识
    user_agent: str    # 带 CDK 的下载信息请求所用的 user_agent
    archive_ext: str   # 发行包格式（需求 §50）

    @property
    def key(self) -> str:
        return f"{self.os_key}-{self.arch_key}"

    @property
    def is_android(self) -> bool:
        return self.os_key == OS_ANDROID


#: 7 组受支持组合（需求 §9 的映射表，已逐条实测，见调研报告 §11.5）
_TARGETS: tuple[Target, ...] = (
    Target(OS_WINDOWS, "x64", RID_DESKTOP, USER_AGENT_DESKTOP, ".zip"),
    Target(OS_LINUX, "x64", RID_DESKTOP, USER_AGENT_DESKTOP, ".tar.gz"),
    Target(OS_LINUX, "arm64", RID_DESKTOP, USER_AGENT_DESKTOP, ".tar.gz"),
    Target(OS_MACOS, "x64", RID_DESKTOP, USER_AGENT_DESKTOP, ".zip"),
    Target(OS_MACOS, "arm64", RID_DESKTOP, USER_AGENT_DESKTOP, ".zip"),
    Target(OS_ANDROID, "aarch64", RID_ANDROID, USER_AGENT_ANDROID, ".apk"),
    Target(OS_ANDROID, "x86_64", RID_ANDROID, USER_AGENT_ANDROID, ".apk"),
)

#: (os_key, arch_key) → Target
TARGETS: dict[tuple[str, str], Target] = {(t.os_key, t.arch_key): t for t in _TARGETS}


def _machine() -> str:
    """归一化的 CPU 架构名（小写）。取不到时返回空串。"""
    try:
        return (_platform.machine() or "").strip().lower()
    except Exception:  # pragma: no cover - 极端环境
        return ""


def current_os() -> str:
    """当前 OS 标识（windows / linux / macos / android）。

    顺序很重要：**Android 必须在 Linux 之前判定**（p4a 的 Python 报 linux）。
    """
    if is_android_strict():
        return OS_ANDROID
    if sys.platform == "win32":
        return OS_WINDOWS
    if sys.platform == "darwin":
        return OS_MACOS
    return OS_LINUX


def normalize_arch(machine: str, os_key: str) -> str:
    """把 :func:`platform.machine` 的取值映射成规格表里的架构字面量。

    未在表内的取值返回空串（调用方据此提示"当前系统架构暂不支持更新"）。
    """
    m = (machine or "").strip().lower()
    if os_key == OS_ANDROID:
        # 真机 arm64-v8a / 模拟器 x86_64；p4a 与 Qt 可能分别报 aarch64 / arm64-v8a
        if m in ("aarch64", "arm64", "arm64-v8a", "armv8l"):
            return "aarch64"
        if m in ("x86_64", "amd64", "x64"):
            return "x86_64"
        return ""
    if os_key == OS_MACOS:
        if m in ("arm64", "aarch64"):
            return "arm64"
        if m in ("x86_64", "amd64", "i386"):
            return "x64"
        return ""
    # Windows / Linux 桌面：只支持 x64 与 arm64（需求 §9 无其他组合）。
    # 32 位 x86 **不映射到 x64**：本轮没有任何 32 位发行包，猜错会下到装不上的包，
    # 按需求 §9 应当明确提示"当前系统架构暂不支持更新"。
    if m in ("x86_64", "amd64", "x64"):
        return "x64"
    if m in ("aarch64", "arm64", "armv8l"):
        return "arm64"
    return ""


def current_target() -> Target | None:
    """当前运行环境对应的 :class:`Target`；不支持时返回 ``None``（需求 §9）。"""
    os_key = current_os()
    arch_key = normalize_arch(_machine(), os_key)
    return TARGETS.get((os_key, arch_key))


def target_for(os_key: str, arch_key: str) -> Target | None:
    """按字面量取目标（供测试与"手动指定平台"使用）。"""
    return TARGETS.get((os_key, arch_key))


def artifact_name(version: str, target: Target) -> str:
    """Release 资产名（需求 §22）。

    ``MangaProof-1.1.0.alpha-windows-x64.zip`` —— 版本号**原样内嵌**
    （``__version__`` 可能带 ``.alpha`` 后缀，不能截断成三段）。
    """
    return f"{ARTIFACT_PREFIX}-{version}-{target.os_key}-{target.arch_key}{target.archive_ext}"


def sha256_manifest_name(version: str) -> str:
    """R2 上的 SHA-256 manifest 文件名（需求 §24：按版本动态生成，不得硬编码）。"""
    return f"{ARTIFACT_PREFIX}-{version}-sha256.json"


def check_url(target: Target, channel: str) -> str:
    """检查更新用的地址（**不带 CDK、不带 user_agent**，需求 §17）。

    7 组地址与需求方给定的清单逐字一致。带 CDK 的"立即更新"请求 =
    本地址追加 ``&cdk=…&user_agent=…``，由 :mod:`mangoproof.update.mirrorchyan`
    在下载阶段构造 —— 检查路径**没有**任何途径传入 CDK（函数签名即边界）。
    """
    return (
        f"{MIRRORCHYAN_BASE}/{target.rid}/latest"
        f"?os={target.os_key}&arch={target.arch_key}&channel={channel}"
    )


def download_url_from_check(check: str, cdk: str, user_agent: str) -> str:
    """在检查地址上追加 CDK 与 user_agent（需求 §18/§20）。

    **只允许**由"立即更新"阶段调用；CDK 为空时调用方必须先拒绝，绝不发请求
    （实测：``cdk=`` 空串会被服务端当成"没带 CDK"，静默返回不含下载字段的响应）。
    """
    if not cdk:
        raise ValueError("CDK 为空：不得发起 MirrorChyan 下载信息请求")
    return f"{check}&cdk={cdk}&user_agent={user_agent}"


def r2_url(base: str, channel: str, filename: str) -> str:
    """Cloudflare R2 下载地址（需求 §23）。"""
    return f"{base.rstrip('/')}/{channel}/{filename}"
