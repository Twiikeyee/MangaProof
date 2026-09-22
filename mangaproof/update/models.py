# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新流程的数据模型。

字段命名尽量与服务端/发布侧保持一致（``version_name`` / ``sha256`` / ``filesize``），
避免在"复制粘贴一次日志"时还要做心算翻译。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mangaproof.update.version import AppVersion


@dataclass(frozen=True)
class ReleaseInfo:
    """一次"目标版本"的完整信息。

    检查阶段（无 CDK）只会填上版本/分支/说明；
    下载阶段的响应（带 CDK）才会带回 ``url`` / ``sha256`` / ``filesize`` 等字段
    （需求 §19）——因此这些都是可选的。
    """

    version: AppVersion
    version_name: str                 # 服务端原始串（含 v 前缀），日志/展示用
    version_number: int = 0           # 服务端资源上传计数：**不参与**版本比较（§3）
    channel: str = "stable"
    os_key: str = ""
    arch_key: str = ""
    release_note: str = ""

    # -- 仅"立即更新"阶段（带 CDK）才有 --
    url: str | None = None            # 服务端下发的短效下载地址（§21：不得自行拼接）
    sha256: str | None = None
    filesize: int | None = None
    update_type: str | None = None
    cdk_expired_time: str | None = None

    @classmethod
    def from_mirrorchyan(
        cls, data: dict, *, channel: str, os_key: str, arch_key: str
    ) -> "ReleaseInfo":
        """从 MirrorChyan 的 ``data`` 构造；版本号无法解析时抛 :class:`InvalidVersion`。"""
        raw_name = str(data.get("version_name", "") or "")
        version = AppVersion.parse(raw_name)

        def _opt_int(key: str) -> int | None:
            value = data.get(key)
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            version=version,
            version_name=raw_name,
            version_number=_opt_int("version_number") or 0,
            channel=str(data.get("channel") or channel),
            os_key=str(data.get("os") or os_key),
            arch_key=str(data.get("arch") or arch_key),
            release_note=str(data.get("release_note") or ""),
            url=(str(data["url"]) if data.get("url") else None),
            sha256=(str(data["sha256"]) if data.get("sha256") else None),
            filesize=_opt_int("filesize"),
            update_type=(str(data["update_type"]) if data.get("update_type") else None),
            cdk_expired_time=(
                str(data["cdk_expired_time"]) if data.get("cdk_expired_time") else None
            ),
        )


@dataclass(frozen=True)
class CheckResult:
    """检查更新的结果（需求 §31~§33 的 UI 数据源）。"""

    current: AppVersion
    release: ReleaseInfo
    #: 检查时使用的渠道设置，用于 UI 回显（"更新分支：stable"）
    branch: str = "stable"

    @property
    def has_update(self) -> bool:
        """是否允许升级：**目标版本 > 当前版本**（需求 §3/§4 的唯一判据）。"""
        return self.release.version > self.current

    @property
    def latest_display(self) -> str:
        return self.release.version.display()

    @property
    def current_display(self) -> str:
        return self.current.display()


@dataclass
class DownloadPlan:
    """"立即更新"阶段确定下来的下载计划（UI 与下载器共用）。"""

    filename: str
    url: str
    sha256: str | None = None
    filesize: int | None = None
    #: 该计划是否允许使用用户配置的代理（MirrorChyan 渠道为 False，需求 §29）
    use_proxy: bool = True
    #: 该计划是否允许限速（MirrorChyan 渠道为 False，需求 §30）
    use_speed_limit: bool = True
    #: 溯源信息，写进日志与诊断
    source: str = "r2"
    notes: list[str] = field(default_factory=list)
