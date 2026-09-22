# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新包校验单测（需求 §53）。

重点覆盖三条来源的哈希写法统一：

- GitHub Release asset 的 ``digest`` = ``sha256:<hex>``（带前缀）；
- R2 manifest（需求 §26）= 裸 hex；
- MirrorChyan 响应 = 裸 hex。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mangaproof.update.checksum import (  # noqa: E402
    ChecksumMismatch,
    normalize_sha256,
    sha256_of,
    verify_file,
    verify_size,
)

BODY = b"MangaProof update package\n" * 100
DIGEST = hashlib.sha256(BODY).hexdigest()


@pytest.fixture
def package(tmp_path: Path) -> Path:
    p = tmp_path / "MangaProof-1.1.0.alpha-linux-x64.tar.gz"
    p.write_bytes(BODY)
    return p


def test_sha256_of(package):
    assert sha256_of(package) == DIGEST


def test_verify_file_bare_hex(package):
    assert verify_file(package, DIGEST) == DIGEST


def test_verify_file_github_digest_prefix(package):
    """GitHub 的 digest 带 sha256: 前缀，必须能直接吃下。"""
    assert verify_file(package, f"sha256:{DIGEST}") == DIGEST


def test_verify_file_uppercase_and_padding(package):
    assert verify_file(package, f"  SHA256:{DIGEST.upper()}  ") == DIGEST


def test_verify_file_mismatch_raises(package):
    with pytest.raises(ChecksumMismatch) as exc:
        verify_file(package, "0" * 64)
    assert exc.value.expected == "0" * 64
    assert exc.value.actual == DIGEST
    # 报错信息里不能带 CDK 之类的东西；这里只确认文件名与两个哈希
    assert package.name in str(exc.value)


@pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "a" * 63, "sha256:"])
def test_normalize_rejects_garbage(bad):
    with pytest.raises(ValueError):
        normalize_sha256(bad)


def test_verify_size(package):
    assert verify_size(package, len(BODY)) is True
    assert verify_size(package, len(BODY) + 1) is False


def test_verify_size_without_expectation(package):
    """服务端没给 filesize 时不参与判定。"""
    assert verify_size(package, None) is True
    assert verify_size(package, 0) is True
