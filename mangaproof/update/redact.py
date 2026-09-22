# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""CDK 等敏感信息脱敏（需求 §16、§77）。

需求原文：

- CDK 不进入普通日志、不进入错误日志、不进入 traceback、
  不出现在下载 URL 日志、不写入诊断报告；
- 需要记录 URL 时写成 ``?cdk=[REDACTED]``。

用法（**记录任何 URL / 字典 / 异常文本之前都必须过一遍**）::

    log.info("请求下载信息：%s", redact_url(url))
    log.debug("响应：%s", redact_mapping(payload))

实现要点：宁可多脱敏，不可漏。除了 ``cdk``，把常见的凭据参数名
（``token`` / ``key`` / ``password`` / ``secret`` / ``authorization``）一并覆盖；
键名匹配大小写不敏感，且兼容 ``api_key`` / ``access_token`` 这类下划线写法。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

REDACTED = "[REDACTED]"

#: 需要脱敏的查询参数/字典键（小写比较；同时匹配 xxx_key / xxx_token 形式）
_SENSITIVE_KEYS: frozenset[str] = frozenset({
    "cdk",
    "token",
    "key",
    "apikey",
    "api_key",
    "access_token",
    "accesskey",
    "access_key",
    "secret",
    "secret_key",
    "secretkey",
    "password",
    "passwd",
    "authorization",
    "auth",
    "credential",
    "signature",
    "sig",
})

#: URL/文本里 ``name=value`` 形式的敏感参数（值截到 & 或空白为止）
_ASSIGN_RE = re.compile(
    r"(?i)\b(" + "|".join(sorted(_SENSITIVE_KEYS, key=len, reverse=True)) + r")=([^&\s\"']+)"
)

#: Authorization 头的常见写法
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def is_sensitive_key(key: str) -> bool:
    """键名是否敏感（大小写不敏感；``api_key`` 这类也命中）。"""
    k = (key or "").strip().lower()
    if k in _SENSITIVE_KEYS:
        return True
    # 形如 mirrorchyan_cdk / x_api_key / my_secret
    parts = [p for p in re.split(r"[^a-z0-9]+", k) if p]
    return any(p in _SENSITIVE_KEYS for p in parts)


def redact_text(text: str) -> str:
    """把任意文本里的敏感 ``key=value`` 与 Bearer 头替换成 ``[REDACTED]``。

    用于日志消息、异常字符串、traceback 文本。
    """
    if not text:
        return text
    out = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)


def redact_url(url: str) -> str:
    """URL 脱敏（需求 §16 的 ``?cdk=[REDACTED]`` 形态）。"""
    return redact_text(url)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """字典脱敏（浅层；嵌套 dict/list 递归处理）。

    用于"要把响应体写进日志"的场景：值一律替换，键名保留，
    这样排查问题时仍看得出服务端返回了哪些字段。
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        if is_sensitive_key(str(key)):
            out[key] = REDACTED
        elif isinstance(value, Mapping):
            out[key] = redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            out[key] = [
                redact_mapping(v) if isinstance(v, Mapping) else v for v in value
            ]
        else:
            out[key] = value
    return out


def redact_exception(exc: BaseException) -> str:
    """异常的单行脱敏文本（用于 ``log`` 与用户可见提示）。

    注意：不要把原始异常对象直接交给日志的 ``exc_info``——traceback 里
    可能带着拼好的 URL（含 CDK）。需要堆栈时用
    ``log.debug(..., exc_info=...)`` 并**先**确认异常文本已脱敏。
    """
    return redact_text(f"{type(exc).__name__}: {exc}")
