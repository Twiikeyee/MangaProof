# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""CDK 脱敏单测（需求 §16）。

需求是"CDK 不进普通日志 / 错误日志 / traceback / URL 日志 / 诊断报告"，
所以这里的用例按"真实会被写进日志的字符串形态"来断言，而不是只测一个函数。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.update.redact import (  # noqa: E402
    REDACTED,
    is_sensitive_key,
    redact_exception,
    redact_mapping,
    redact_text,
    redact_url,
)

SECRET = "0001bf520b5a75eb3e61f458"


def test_url_cdk_is_redacted():
    url = (
        "https://mirrorchyan.com/api/resources/MangaProof/latest"
        f"?os=linux&arch=x64&channel=stable&cdk={SECRET}&user_agent=MangaProofUpgrade"
    )
    out = redact_url(url)
    assert SECRET not in out
    assert f"cdk={REDACTED}" in out
    # 非敏感参数保持原样，便于排查
    assert "os=linux" in out and "arch=x64" in out and "channel=stable" in out
    assert "user_agent=MangaProofUpgrade" in out


def test_redact_text_handles_multiple_params():
    text = f"GET /x?a=1&cdk={SECRET}&token=abc&b=2"
    out = redact_text(text)
    assert SECRET not in out and "abc" not in out
    assert "a=1" in out and "b=2" in out


def test_bearer_header_is_redacted():
    out = redact_text(f"Authorization: Bearer {SECRET}")
    assert SECRET not in out
    assert REDACTED in out


def test_redact_keeps_non_secret_values():
    payload = {
        "version_name": "v1.0.0",
        "version_number": 2,
        "url": "https://mirrorchyan.com/api/resources/download/AbC123",
        "sha256": "a" * 64,
    }
    out = redact_mapping(payload)
    assert out == payload


def test_redact_mapping_masks_sensitive_keys_recursively():
    payload = {
        "cdk": SECRET,
        "mirrorchyan_cdk": SECRET,
        "nested": {"api_key": SECRET, "ok": 1},
        "items": [{"access_token": SECRET, "name": "x"}],
        "user_agent": "MangaProofUpgrade",
    }
    out = redact_mapping(payload)
    assert out["cdk"] == REDACTED
    assert out["mirrorchyan_cdk"] == REDACTED
    assert out["nested"]["api_key"] == REDACTED
    assert out["nested"]["ok"] == 1
    assert out["items"][0]["access_token"] == REDACTED
    assert out["items"][0]["name"] == "x"
    # user_agent 不是凭据，保留（排查时要看它）
    assert out["user_agent"] == "MangaProofUpgrade"


def test_exception_text_is_redacted():
    exc = RuntimeError(f"下载失败：https://h/p?cdk={SECRET}")
    out = redact_exception(exc)
    assert SECRET not in out
    assert "RuntimeError" in out


def test_is_sensitive_key_variants():
    for key in ("cdk", "CDK", "api_key", "ApiKey", "mirrorchyan_cdk",
                "access_token", "X-Secret", "password", "authorization"):
        assert is_sensitive_key(key), key
    for key in ("version_name", "user_agent", "channel", "sha256", "os", "arch"):
        assert not is_sensitive_key(key), key
