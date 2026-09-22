# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MirrorChyan 客户端的单测（需求 §17/§18/§19/§20/§21）。

全部用 ``httpx.MockTransport`` 离线跑，**不联网**：

- 检查请求逐字断言"不含 cdk / 不含 user_agent"（需求 §17 的结构性保证）；
- 下载信息请求逐字断言带上了 cdk 与 user_agent（需求 §18/§20）；
- 错误码 → 用户文案的映射（官方 ErrorCode.md）；
- 版本号无法解析（真实案例：alpha 通道返回字面量 "alpha"）必须抛错，
  不能被当成"已是最新版本"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.update import detector  # noqa: E402
from mangaproof.update.errors import (  # noqa: E402
    MirrorChyanError,
    NetworkError,
    VersionInfoError,
)
from mangaproof.update.mirrorchyan import check_update, fetch_download_info  # noqa: E402
from mangaproof.update.version import is_newer  # noqa: E402

TARGET = detector.target_for("linux", "x64")
SECRET_CDK = "0001bf520b5a75eb3e61f458"


def make_transport(payload: dict, *, status: int = 200, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def ok_payload(**data) -> dict:
    body = {
        "version_name": "v1.0.0",
        "version_number": 2,
        "channel": "stable",
        "os": "linux",
        "arch": "amd64",
        "release_note": "",
    }
    body.update(data)
    return {"code": 0, "msg": "success", "data": body}


# --- 检查阶段（零凭据） -----------------------------------------------------

def test_check_uses_no_credentials():
    """需求 §17：检查请求不带 CDK、不带 user_agent。"""
    seen: list[httpx.Request] = []
    transport = make_transport(ok_payload(), record=seen)

    release, current = check_update(
        target=TARGET, channel="stable", current_version="1.1.0.alpha",
        transport=transport,
    )

    assert len(seen) == 1
    url = str(seen[0].url)
    assert "cdk" not in url
    assert "user_agent" not in url
    assert url == detector.check_url(TARGET, "stable")
    assert release.version_name == "v1.0.0"
    assert release.version_number == 2
    assert current == "1.1.0.alpha"
    # 目标是 v1.0.0，当前是 1.1.0.alpha → 不更新
    assert is_newer(release.version, current) is False


def test_check_ignores_echoed_normalized_values():
    """服务端会归一化回显（x64→amd64、macos→darwin），不得据此反推请求参数。"""
    payload = ok_payload(os="darwin", arch="amd64")
    release, _ = check_update(
        target=detector.target_for("macos", "x64"), channel="stable",
        current_version="1.0.0", transport=make_transport(payload),
    )
    assert release.os_key == "darwin"      # 原样记录服务端回显（仅用于日志核对）
    assert detector.check_url(detector.target_for("macos", "x64"), "stable").find(
        "os=macos"
    ) > 0


def test_check_detects_newer_version():
    release, current = check_update(
        target=TARGET, channel="beta", current_version="1.0.0",
        transport=make_transport(ok_payload(version_name="v1.1.0.beta", channel="beta")),
    )
    assert is_newer(release.version, current) is True


def test_check_maps_business_error_code():
    transport = make_transport({"code": 8001, "msg": "resource not found"})
    with pytest.raises(MirrorChyanError) as exc:
        check_update(
            target=TARGET, channel="stable", current_version="1.0.0",
            transport=transport,
        )
    assert exc.value.code == 8001
    assert "发行包" in str(exc.value)


@pytest.mark.parametrize(
    "code,keyword",
    [
        (7001, "过期"),
        (7002, "CDK 无效"),
        (7003, "上限"),
        (7004, "不匹配"),
        (7005, "封禁"),
        (8004, "分支"),
    ],
)
def test_error_code_messages(code, keyword):
    err = MirrorChyanError(code, "server says something")
    assert keyword in str(err)


def test_unknown_error_code_falls_back_to_server_message():
    err = MirrorChyanError(9999, "some server text")
    assert "some server text" in str(err)


def test_negative_code_is_reported_as_severe():
    err = MirrorChyanError(-1, "boom")
    assert "严重错误" in str(err)


def test_unparseable_version_raises_not_silently_up_to_date():
    """真实案例：alpha 通道返回字面量 "alpha" → 必须报错，不能报"已是最新"。"""
    transport = make_transport(ok_payload(version_name="alpha", channel="alpha"))
    with pytest.raises(VersionInfoError) as exc:
        check_update(
            target=TARGET, channel="alpha", current_version="1.1.0.alpha",
            transport=transport,
        )
    assert "alpha" in str(exc.value)
    assert "异常" in str(exc.value)


def test_http_error_becomes_network_error_without_secrets():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(NetworkError):
        check_update(
            target=TARGET, channel="stable", current_version="1.0.0",
            transport=httpx.MockTransport(handler),
        )


def test_invalid_json_becomes_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(NetworkError):
        check_update(
            target=TARGET, channel="stable", current_version="1.0.0",
            transport=httpx.MockTransport(handler),
        )


# --- 下载阶段（带 CDK） -----------------------------------------------------

def test_fetch_download_info_sends_cdk_and_user_agent():
    seen: list[httpx.Request] = []
    payload = ok_payload(
        url="https://mirrorchyan.com/api/resources/download/AbCdEf",
        sha256="a" * 64,
        filesize=176652385,
        update_type="full",
        cdk_expired_time="2027-01-01T00:00:00Z",
    )
    transport = make_transport(payload, record=seen)

    release = fetch_download_info(
        target=TARGET, channel="stable", cdk=SECRET_CDK, transport=transport
    )

    url = str(seen[0].url)
    assert f"cdk={SECRET_CDK}" in url
    assert "user_agent=MangaProofUpgrade" in url
    assert release.url == "https://mirrorchyan.com/api/resources/download/AbCdEf"
    assert release.sha256 == "a" * 64
    assert release.filesize == 176652385
    assert release.update_type == "full"
    assert release.cdk_expired_time == "2027-01-01T00:00:00Z"


def test_android_download_info_uses_android_user_agent():
    seen: list[httpx.Request] = []
    payload = ok_payload(
        url="https://mirrorchyan.com/api/resources/download/x", sha256="b" * 64
    )
    fetch_download_info(
        target=detector.target_for("android", "aarch64"),
        channel="stable",
        cdk=SECRET_CDK,
        transport=make_transport(payload, record=seen),
    )
    url = str(seen[0].url)
    assert "os=android&arch=aarch64" in url
    assert "user_agent=MangaProof_exec" in url


def test_empty_cdk_is_refused_before_request():
    """空 CDK 不发请求：实测会被服务端当成"没带 CDK"静默降级。"""
    seen: list[httpx.Request] = []
    with pytest.raises(ValueError):
        fetch_download_info(
            target=TARGET, channel="stable", cdk="",
            transport=make_transport(ok_payload(), record=seen),
        )
    assert seen == []


def test_missing_url_in_response_is_an_error():
    with pytest.raises(NetworkError):
        fetch_download_info(
            target=TARGET, channel="stable", cdk=SECRET_CDK,
            transport=make_transport(ok_payload()),
        )


def test_cdk_never_appears_in_error_text():
    """需求 §16：CDK 不进错误日志/traceback。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect: {request.url}")

    with pytest.raises(NetworkError) as exc:
        fetch_download_info(
            target=TARGET, channel="stable", cdk=SECRET_CDK,
            transport=httpx.MockTransport(handler),
        )
    assert SECRET_CDK not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


def test_mirrorchyan_client_ignores_env_proxy(monkeypatch):
    """需求 §29：MirrorChyan 不使用用户配置的代理（连环境变量也不看）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    seen: list[httpx.Request] = []
    check_update(
        target=TARGET, channel="stable", current_version="1.0.0",
        transport=make_transport(ok_payload(), record=seen),
    )
    # MockTransport 收到请求即说明没有被环境变量代理拦走；
    # 同时确认 client 关闭了 trust_env（构造处断言，避免只测到表象）
    from mangaproof.update import mirrorchyan

    with mirrorchyan._client() as client:
        assert client.trust_env is False
