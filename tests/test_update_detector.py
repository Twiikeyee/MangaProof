# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""平台/架构识别与 7 组 MirrorChyan 映射的单测（需求 §9、§22、§23）。

这些断言是**规格的字面固化**：请求 URL、资产名、user_agent 都逐字比对，
防止后来"顺手改个大小写"把服务端契约改坏（服务端会把 x64 归一化成 amd64 回显，
但**请求必须用规格表里的字面量**）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.update import detector  # noqa: E402


@pytest.fixture
def fake_platform(monkeypatch):
    """伪造运行平台（is_android_strict 是直接导入的，必须打在 detector 上）。"""

    def _apply(*, android: bool, sys_platform: str, machine: str):
        monkeypatch.setattr(detector, "is_android_strict", lambda: android)
        monkeypatch.setattr(detector.sys, "platform", sys_platform)
        monkeypatch.setattr(detector, "_machine", lambda: machine)

    return _apply


@pytest.mark.parametrize(
    "os_key,arch_key,url",
    [
        ("windows", "x64",
         "https://mirrorchyan.com/api/resources/MangaProof/latest"
         "?os=windows&arch=x64&channel=stable"),
        ("linux", "x64",
         "https://mirrorchyan.com/api/resources/MangaProof/latest"
         "?os=linux&arch=x64&channel=stable"),
        ("linux", "arm64",
         "https://mirrorchyan.com/api/resources/MangaProof/latest"
         "?os=linux&arch=arm64&channel=stable"),
        ("macos", "x64",
         "https://mirrorchyan.com/api/resources/MangaProof/latest"
         "?os=macos&arch=x64&channel=stable"),
        ("macos", "arm64",
         "https://mirrorchyan.com/api/resources/MangaProof/latest"
         "?os=macos&arch=arm64&channel=stable"),
        ("android", "aarch64",
         "https://mirrorchyan.com/api/resources/MangaProof_exec/latest"
         "?os=android&arch=aarch64&channel=stable"),
        ("android", "x86_64",
         "https://mirrorchyan.com/api/resources/MangaProof_exec/latest"
         "?os=android&arch=x86_64&channel=stable"),
    ],
)
def test_seven_check_urls(os_key, arch_key, url):
    """7 组地址逐字比对（需求方给定的清单）。"""
    target = detector.target_for(os_key, arch_key)
    assert target is not None
    assert detector.check_url(target, "stable") == url


def test_channel_is_substituted_dynamically():
    target = detector.target_for("linux", "x64")
    for channel in ("stable", "beta", "alpha"):
        assert detector.check_url(target, channel).endswith(f"&channel={channel}")


def test_check_url_never_carries_credentials():
    """检查地址里不得出现 cdk / user_agent（需求 §17/§20）。"""
    for target in detector.TARGETS.values():
        url = detector.check_url(target, "stable")
        assert "cdk" not in url
        assert "user_agent" not in url


def test_only_seven_supported_targets():
    assert len(detector.TARGETS) == 7


def test_download_url_appends_cdk_and_user_agent():
    target = detector.target_for("windows", "x64")
    url = detector.download_url_from_check(
        detector.check_url(target, "stable"), "CDK123", target.user_agent
    )
    assert url.endswith("&cdk=CDK123&user_agent=MangaProofUpgrade")


def test_download_url_refuses_empty_cdk():
    """CDK 为空必须拒绝——实测空串会被服务端当成"没带 CDK"静默降级。"""
    target = detector.target_for("linux", "x64")
    with pytest.raises(ValueError):
        detector.download_url_from_check(
            detector.check_url(target, "stable"), "", target.user_agent
        )


def test_android_uses_its_own_rid_and_user_agent():
    android = detector.target_for("android", "aarch64")
    desktop = detector.target_for("linux", "x64")
    assert android.rid == "MangaProof_exec"
    assert android.user_agent == "MangaProof_exec"
    assert desktop.rid == "MangaProof"
    assert desktop.user_agent == "MangaProofUpgrade"


@pytest.mark.parametrize(
    "os_key,arch_key,filename",
    [
        ("windows", "x64", "MangaProof-1.1.0.alpha-windows-x64.zip"),
        ("linux", "x64", "MangaProof-1.1.0.alpha-linux-x64.tar.gz"),
        ("linux", "arm64", "MangaProof-1.1.0.alpha-linux-arm64.tar.gz"),
        ("macos", "x64", "MangaProof-1.1.0.alpha-macos-x64.zip"),
        ("macos", "arm64", "MangaProof-1.1.0.alpha-macos-arm64.zip"),
        ("android", "aarch64", "MangaProof-1.1.0.alpha-android-aarch64.apk"),
        ("android", "x86_64", "MangaProof-1.1.0.alpha-android-x86_64.apk"),
    ],
)
def test_artifact_names_match_ci(os_key, arch_key, filename):
    """资产名必须与 .github/workflows 的产物命名一致（需求 §22/§50）。"""
    target = detector.target_for(os_key, arch_key)
    assert detector.artifact_name("1.1.0.alpha", target) == filename


def test_version_is_embedded_verbatim():
    """版本号原样内嵌，不能截断成三段（1.1.0.alpha 不是 1.1.0）。"""
    target = detector.target_for("linux", "x64")
    assert "1.1.0.alpha" in detector.artifact_name("1.1.0.alpha", target)


def test_manifest_name_is_dynamic():
    assert detector.sha256_manifest_name("1.0.0") == "MangaProof-1.0.0-sha256.json"
    assert (
        detector.sha256_manifest_name("1.1.0.alpha")
        == "MangaProof-1.1.0.alpha-sha256.json"
    )


def test_r2_url_shape():
    assert detector.r2_url(
        "https://download.mangaproof.priloba.com/", "stable", "x.zip"
    ) == "https://download.mangaproof.priloba.com/stable/x.zip"


@pytest.mark.parametrize(
    "sys_platform,machine,expected",
    [
        ("win32", "AMD64", ("windows", "x64")),
        ("linux", "x86_64", ("linux", "x64")),
        ("linux", "aarch64", ("linux", "arm64")),
        ("darwin", "arm64", ("macos", "arm64")),
        ("darwin", "x86_64", ("macos", "x64")),
    ],
)
def test_current_target_on_desktops(fake_platform, sys_platform, machine, expected):
    fake_platform(android=False, sys_platform=sys_platform, machine=machine)
    target = detector.current_target()
    assert target is not None and target.key == "-".join(expected)


@pytest.mark.parametrize(
    "machine,expected",
    [("aarch64", "aarch64"), ("arm64-v8a", "aarch64"), ("x86_64", "x86_64")],
)
def test_android_is_detected_before_linux(fake_platform, machine, expected):
    """Android 的 Python 报 sys.platform == "linux"，判定顺序不能反。"""
    fake_platform(android=True, sys_platform="linux", machine=machine)
    target = detector.current_target()
    assert target is not None and target.key == f"android-{expected}"


@pytest.mark.parametrize("machine", ["i686", "mips", "riscv64", ""])
def test_unsupported_arch_returns_none(fake_platform, machine):
    """需求 §9：没有对应发行包时返回 None，由 UI 提示"暂不支持更新"。"""
    fake_platform(android=False, sys_platform="linux", machine=machine)
    assert detector.current_target() is None
