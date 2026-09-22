# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MirrorChyan 客户端（需求 §17~§21）。

**凭据边界（本项目最重要的一条约束）**：

- :func:`check_update` 的签名里**没有** cdk 形参 —— 检查阶段零凭据，
  不是靠"记得别传"，而是靠函数签名从类型层面杜绝；
- 只有 :func:`fetch_download_info` 接受 cdk，且它**只允许**在用户点击
  "立即更新"之后调用（需求 §18：「发现新版本 + 用户选择 MirrorChyan +
  用户点击立即更新」三者同时成立才使用 CDK）；
- 所有日志走 :mod:`mangaproof.update.redact`，URL 只以脱敏形式出现（需求 §16）。

**代理**：MirrorChyan 不使用用户配置的代理（需求 §29），
因此这里的 httpx client 一律 ``trust_env=False``，连环境变量里的
``HTTP_PROXY`` 也不看 —— 否则"设置了代理"会悄悄改变检查链路的行为。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mangaproof.update import detector
from mangaproof.update.errors import MirrorChyanError, NetworkError, VersionInfoError
from mangaproof.update.models import ReleaseInfo
from mangaproof.update.redact import redact_url
from mangaproof.update.version import InvalidVersion

log = logging.getLogger("mangaproof.update.mirrorchyan")

#: API 调用的超时（连接/读取分开；检查接口很小，读取给 20 秒足够）
API_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)

#: 检查/下载信息都用的请求头。注意 user_agent **参数**是 MirrorChyan 的业务参数，
#: 与 HTTP 头不是一回事：前者在下载阶段作为 query 传递（需求 §20）。
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "MangaProof",
}


def _client(*, timeout: httpx.Timeout = API_TIMEOUT,
            transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """MirrorChyan 专用 client：不跟随用户代理设置，跟随重定向。"""
    return httpx.Client(
        timeout=timeout,
        headers=_HEADERS,
        follow_redirects=True,
        trust_env=False,        # 需求 §29：MirrorChyan 不使用用户配置的代理
        transport=transport,    # 测试注入 MockTransport
    )


def _get_json(
    client: httpx.Client, url: str, *, action: str
) -> dict[str, Any]:
    """GET 并解析 JSON；网络异常统一转成 :class:`NetworkError`（URL 已脱敏）。"""
    try:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise NetworkError(
            action, f"HTTP {exc.response.status_code}（{redact_url(str(exc.request.url))}）"
        ) from exc
    except httpx.HTTPError as exc:
        raise NetworkError(action, f"{type(exc).__name__}: {exc}") from exc
    except ValueError as exc:   # JSON 解析失败
        raise NetworkError(action, f"响应不是合法 JSON：{exc}") from exc

    if not isinstance(payload, dict):
        raise NetworkError(action, "响应结构异常（不是 JSON 对象）")
    return payload


def _unwrap(payload: dict[str, Any], *, action: str) -> dict[str, Any]:
    """校验 ``code == 0`` 并取出 ``data``（业务错误一律 HTTP 200 + body.code）。"""
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError):
        raise NetworkError(action, "响应缺少 code 字段") from None
    if code != 0:
        # 错误码语义见官方 ErrorCode.md；此处不打印 data（可能含 cdk 相关字段）
        raise MirrorChyanError(code, str(payload.get("msg", "")))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise NetworkError(action, "响应缺少 data 字段")
    return data


def _release_from(
    data: dict[str, Any], *, channel: str, target: detector.Target
) -> ReleaseInfo:
    """构造 :class:`ReleaseInfo`，并把"版本号不可解析"转成明确错误。

    这里必须转换而不是让它冒泡成 :class:`InvalidVersion`：调用方（UI）
    只认识 :class:`UpdateError` 一族，而"服务端版本信息异常"要有自己的文案
    （真实案例：alpha 通道返回字面量 ``"alpha"``）。
    """
    try:
        return ReleaseInfo.from_mirrorchyan(
            data, channel=channel, os_key=target.os_key, arch_key=target.arch_key
        )
    except InvalidVersion as exc:
        raise VersionInfoError(str(data.get("version_name", "") or ""), channel) from exc


def check_update(
    *,
    target: detector.Target,
    channel: str,
    current_version: str,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ReleaseInfo, str]:
    """检查更新（**不带 CDK**，需求 §17）。

    :returns: ``(ReleaseInfo, current_version)`` —— 由调用方用
        :func:`mangaproof.update.version.is_newer` 判定，或直接用
        :class:`mangaproof.update.models.CheckResult` 包装。

    注意签名：**没有 cdk**。这是需求 §17「检查更新阶段不携带 CDK」的结构性保证。
    """
    url = detector.check_url(target, channel)
    action = "检查更新"
    log.info("检查更新：%s（分支 %s）", redact_url(url), channel)

    with _client(transport=transport) as client:
        payload = _get_json(client, url, action=action)
    data = _unwrap(payload, action=action)

    raw_name = str(data.get("version_name", "") or "")
    release = _release_from(data, channel=channel, target=target)

    log.info(
        "检查完成：服务端 %s（version_number=%s，分支 %s），当前 %s",
        release.version_name, release.version_number, channel, current_version,
    )
    return release, current_version


def fetch_download_info(
    *,
    target: detector.Target,
    channel: str,
    cdk: str,
    transport: httpx.BaseTransport | None = None,
) -> ReleaseInfo:
    """取下载信息（**带 CDK**，需求 §18~§21）。

    - 地址 = 检查地址 + ``&cdk=…&user_agent=…``（user_agent 见需求 §20）；
    - 返回里的 ``url`` 是**服务端下发的短效地址**，客户端不得自行拼接（§21）；
    - CDK 为空时直接拒绝：实测空串会被服务端当成"没带 CDK"，
      静默返回一份不含 ``url``/``sha256`` 的响应，看起来像"没有下载包"。
    """
    url = detector.download_url_from_check(
        detector.check_url(target, channel), cdk, target.user_agent
    )
    action = "获取下载信息"
    log.info(
        "获取下载信息：%s（分支 %s，rid %s，user_agent %s）",
        redact_url(url), channel, target.rid, target.user_agent,
    )

    with _client(transport=transport) as client:
        payload = _get_json(client, url, action=action)
    data = _unwrap(payload, action=action)

    release = _release_from(data, channel=channel, target=target)
    if not release.url:
        # code==0 但没有 url：按需求 §19 属于异常响应
        raise NetworkError(action, "服务端未返回下载地址（url 为空）")
    return release
