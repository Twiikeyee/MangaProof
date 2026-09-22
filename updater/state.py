# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""安装器阶段状态与成功标记（需求 §58/§59/§64，调研报告 §5.4）。

两个文件，职责不同，**不要混用**：

``--status-file``（安装器状态）
    安装器每进入一个阶段就重写一次，供"上次更新没做完"的判断（§64）与主程序
    读取进度/失败原因。任务书要求主程序传一个**持久目录**下的路径：
    ``/tmp``、``%TEMP%`` 会被系统清理（调研报告 §5.4），状态文件丢了就无法在
    断电后判断更新是否完成。文件名由主程序侧固定为 ``installer-state.json``
    （见 :data:`STATE_FILE_NAME`）——安装器**只读写这一个状态文件**，不另造名字。

``--success-marker``（成功标记）
    新版主程序在"启动 + 核心初始化 + 旧数据加载成功"之后主动写（§57/§58），
    放在**更新临时目录**里。内容（主程序侧契约）::

        {"token": "…", "version": "…", "pid": 12345, "ts": 1700000000.0}

    安装器只认 token 与 version **同时**匹配的标记（§59/§61）：只判断"文件存在"
    会把上一次更新的残留标记当成成功，不幂等。

写入一律"临时文件 + :func:`os.replace`"（原子落盘）：安装器随时可能被杀
（§64），状态文件绝不能出现半截 JSON。

安全：状态里**不得出现任何 CDK**（需求 §16/§77）。:meth:`StateStore.save`
对键名做递归检查，写进去就报错。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("mangaproof.updater.state")

#: 状态机（需求 §78 的安装器部分，顺序即合法推进顺序）
PHASES: tuple[str, ...] = (
    "INIT",
    "VALIDATE",
    "BACKUP_DATA",
    "VERIFY_PACKAGE",
    "RENAME_OLD",
    "EXTRACT",
    "RESTORE_DATA",
    "LAUNCH_NEW",
    "WAIT_SUCCESS",
    "SUCCESS",
    "CLEANUP",
    "ROLLBACK",
    "FAILED",
)

#: 阶段的中文文案（安装器 UI 第一层信息，调研报告 §11.4）
PHASE_LABELS: dict[str, str] = {
    "INIT": "正在初始化安装器…",
    "VALIDATE": "正在校验安装参数…",
    "BACKUP_DATA": "正在备份用户数据…",
    "VERIFY_PACKAGE": "正在校验更新包…",
    "RENAME_OLD": "正在重命名旧版本…",
    "EXTRACT": "正在解压新版本…",
    "RESTORE_DATA": "正在恢复用户数据…",
    "LAUNCH_NEW": "正在启动新版本…",
    "WAIT_SUCCESS": "等待新版本写入成功标记（最长 60 秒）…",
    "SUCCESS": "更新成功",
    "CLEANUP": "正在清理临时文件…",
    "ROLLBACK": "正在回滚到旧版本…",
    "FAILED": "更新失败",
}

#: 已发生破坏性改动（改名/解压）的阶段——这些阶段起失败**必须**回滚（§62/§63）
DESTRUCTIVE_PHASES: frozenset[str] = frozenset(
    {"RENAME_OLD", "EXTRACT", "RESTORE_DATA", "LAUNCH_NEW", "WAIT_SUCCESS"}
)

TERMINAL_PHASES: frozenset[str] = frozenset({"SUCCESS", "FAILED"})

#: 状态文件里不允许出现的键名片段（需求 §16/§77：CDK 等敏感凭据必须脱敏）
FORBIDDEN_KEY_FRAGMENTS: tuple[str, ...] = ("cdk",)


class StateError(Exception):
    """状态文件读写失败，或状态内容违反安全约束。"""


def label_for(phase: str) -> str:
    """阶段的中文文案；未知阶段原样返回（UI 不得因此崩溃）。"""
    return PHASE_LABELS.get(phase, phase)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def assert_no_secrets(payload: Any, *, path: str = "$") -> None:
    """递归检查键名，出现 CDK 之类敏感键即报错（需求 §16）。"""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            text = str(key)
            low = text.lower()
            if any(frag in low for frag in FORBIDDEN_KEY_FRAGMENTS):
                raise StateError(
                    f"状态中不允许出现敏感字段：{path}.{text}（需求 §16：CDK 不得落盘/入日志）"
                )
            assert_no_secrets(value, path=f"{path}.{text}")
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            assert_no_secrets(value, path=f"{path}[{index}]")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写文件：同目录临时文件 → flush+fsync → :func:`os.replace`。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """原子写 JSON（UTF-8、缩进 2、结尾换行：便于人工排查）。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    atomic_write_bytes(path, (text + "\n").encode("utf-8"))


def read_json(path: Path) -> dict[str, Any] | None:
    """读 JSON；文件不存在/内容损坏返回 ``None``（调用方决定如何降级）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class UpdateState:
    """安装器状态（§64 恢复所需的最小集合：token/version/各路径/pid/阶段）。"""

    phase: str = "INIT"
    token: str = ""
    version: str = ""
    install_dir: str = ""
    old_dir: str = ""
    package: str = ""
    data_backup: str = ""
    success_marker: str = ""
    status_file: str = ""
    platform: str = ""
    parent_pid: int = 0
    child_pid: int = 0
    #: 校验通过的更新包 SHA-256（``--sha256`` 为空时是空串；只作诊断用）
    package_sha256: str = ""
    #: 是否已经执行了 ``MangaProof → MangaProof.old``（回滚的前置条件）
    renamed_old: bool = False
    message: str = ""
    error: str = ""
    rollback_reason: str = ""
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "token": self.token,
            "version": self.version,
            "install_dir": self.install_dir,
            "old_dir": self.old_dir,
            "package": self.package,
            "data_backup": self.data_backup,
            "success_marker": self.success_marker,
            "status_file": self.status_file,
            "platform": self.platform,
            "parent_pid": int(self.parent_pid),
            "child_pid": int(self.child_pid),
            "package_sha256": self.package_sha256,
            "renamed_old": bool(self.renamed_old),
            "message": self.message,
            "error": self.error,
            "rollback_reason": self.rollback_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UpdateState":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in dict(data).items() if k in known}
        payload["history"] = list(payload.get("history") or [])
        for key in ("parent_pid", "child_pid"):
            try:
                payload[key] = int(payload.get(key) or 0)
            except (TypeError, ValueError):
                payload[key] = 0
        return cls(**payload)

    def touch(self, phase: str | None = None, *, message: str = "", **fields: Any) -> None:
        """推进阶段并追加一条历史（阶段+时间+文案，供 UI/日志回放）。"""
        if phase:
            self.phase = phase
        for key, value in fields.items():
            setattr(self, key, value)
        if message:
            self.message = message
        self.updated_at = _utc_now()
        entry: dict[str, Any] = {
            "phase": self.phase,
            "at": self.updated_at,
            "label": label_for(self.phase),
        }
        if message:
            entry["message"] = message
        self.history.append(entry)


class StateStore:
    """状态文件的原子读写（``path`` 为空时静默降级为 no-op）。"""

    def __init__(self, path: Path | str | None):
        self.path = Path(path) if path else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def save(self, state: UpdateState) -> None:
        if self.path is None:
            return
        payload = state.to_dict()
        assert_no_secrets(payload)
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:  # 状态写不进去不能阻断安装（例如目录只读）
            log.warning("状态文件写入失败：%s（%s）", self.path, exc)

    def transition(
        self, state: UpdateState, phase: str, *, message: str = "", **fields: Any
    ) -> UpdateState:
        state.touch(phase, message=message, **fields)
        self.save(state)
        return state

    def load(self) -> dict[str, Any] | None:
        if self.path is None:
            return None
        return read_json(self.path)


# --------------------------------------------------------------------------- #
# 成功标记（需求 §57~§59）
# --------------------------------------------------------------------------- #

#: 状态文件名（主程序侧的约定；安装器只读写 ``--status-file`` 指向的这一个文件）
STATE_FILE_NAME = "installer-state.json"

#: 新版主程序启动参数的 flag 约定（安装器 → 主程序，**跨进程契约，不要改名**）
ARG_TOKEN = "--update-token"
ARG_MARKER = "--success-marker"
ARG_VERSION = "--update-version"

MARKER_TOKEN_KEY = "token"
MARKER_VERSION_KEY = "version"


def normalize_version(value: str) -> str:
    """版本号归一：去空白、去前缀 ``v``、小写（``v1.1.0.alpha`` == ``1.1.0.Alpha``）。"""
    return (value or "").strip().lstrip("vV").lower()


@dataclass(frozen=True)
class MarkerCheck:
    """成功标记的检查结果（``valid`` 才允许判定更新成功）。"""

    exists: bool
    valid: bool
    reason: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def token(self) -> str:
        return str(self.data.get(MARKER_TOKEN_KEY, ""))

    @property
    def version(self) -> str:
        return str(self.data.get(MARKER_VERSION_KEY, ""))


def read_marker(path: Path) -> dict[str, Any] | None:
    """读成功标记。

    主格式是 JSON（:func:`write_marker`）；同时容忍 ``key=value`` 文本行，
    方便主程序在极端情况下用一行 shell 写标记。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = raw.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    parsed: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed or None


def check_marker(path: Path, token: str, version: str) -> MarkerCheck:
    """校验成功标记：**必须 token 与 version 同时匹配**（§59/§61）。

    只判存在是不幂等的：上一次更新的残留标记会导致误判成功（调研报告 §5.4）。
    """
    data = read_marker(path)
    if data is None:
        return MarkerCheck(exists=False, valid=False, reason="成功标记尚未出现")
    if not data:
        return MarkerCheck(exists=True, valid=False, reason="成功标记为空")
    got_token = str(data.get(MARKER_TOKEN_KEY, ""))
    got_version = str(data.get(MARKER_VERSION_KEY, ""))
    if not token:
        return MarkerCheck(
            exists=True, valid=False, reason="安装器没有 token，无法校验成功标记", data=data
        )
    if got_token != token:
        return MarkerCheck(
            exists=True, valid=False, reason="成功标记的 token 不匹配（可能是上次更新的残留）", data=data
        )
    if normalize_version(got_version) != normalize_version(version):
        return MarkerCheck(
            exists=True,
            valid=False,
            reason=(
                f"成功标记的版本不匹配：期望 {version!r}，实际 {got_version!r}"
            ),
            data=data,
        )
    return MarkerCheck(exists=True, valid=True, reason="成功标记校验通过", data=data)


def write_marker(
    path: Path,
    token: str,
    version: str,
    *,
    pid: int = 0,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """原子写成功标记（安装器侧只读；主程序侧写的就是这个格式）。

    内容与主程序侧的约定一致（跨进程契约）::

        {"token": "…", "version": "…", "pid": 12345, "ts": 1700000000.0}

    安装器 :func:`check_marker` 只比较 ``token`` 与 ``version`` 两个键，
    其余键原样保留，方便主程序加诊断信息。
    """
    payload: dict[str, Any] = {
        MARKER_TOKEN_KEY: token,
        MARKER_VERSION_KEY: version,
        "pid": int(pid),
        "ts": time.time(),
    }
    if extra:
        payload.update(dict(extra))
    assert_no_secrets(payload)
    atomic_write_json(Path(path), payload)


def remove_marker(path: Path) -> bool:
    """删除成功标记（§61 清理清单之一）；返回是否真的删掉了。"""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("成功标记删除失败：%s（%s）", path, exc)
        return False


__all__ = [
    "ARG_MARKER",
    "ARG_TOKEN",
    "ARG_VERSION",
    "DESTRUCTIVE_PHASES",
    "MARKER_TOKEN_KEY",
    "MARKER_VERSION_KEY",
    "PHASES",
    "PHASE_LABELS",
    "STATE_FILE_NAME",
    "TERMINAL_PHASES",
    "MarkerCheck",
    "StateError",
    "StateStore",
    "UpdateState",
    "assert_no_secrets",
    "atomic_write_bytes",
    "atomic_write_json",
    "check_marker",
    "label_for",
    "normalize_version",
    "read_json",
    "read_marker",
    "remove_marker",
    "write_marker",
]
