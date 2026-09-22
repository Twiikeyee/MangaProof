# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新包结构校验与安全解压单测（需求 §50~§52，调研报告 §5.2/§5.3）。

两条必须守住的行为：

1. **恶意归档一律在落盘前被拒**：``../``、绝对路径、软链接逃逸、设备文件；
2. **合法归档要正确还原 POSIX 语义**：exec 位与软链接必须保留（macOS 的
   ``.app`` 依赖交叉软链；Linux 的 ``MangaProof`` 必须有执行位）。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from updater import archive
from updater.archive import ArchiveError

import test_updater_support as sup  # noqa: E402  (tests/ 已在 sys.path 上)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位/软链接语义")


# --------------------------------------------------------------------------- #
# 合法包：正常解压 + 保留 exec 位与软链接
# --------------------------------------------------------------------------- #


def test_tar_gz_extracts_and_keeps_exec_bit_and_symlink(tmp_path: Path):
    pkg = sup.write_tar_gz(
        tmp_path / "pkg.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"#!/bin/sh\n"),
            sup.ArchiveEntry("MangaProof/_internal/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/_internal/libfoo.so", "file", 0o644, b"LIB"),
            sup.ArchiveEntry("MangaProof/_internal/link.so", "sym", link="libfoo.so"),
        ],
    )
    dest = tmp_path / "out"
    result = archive.safe_extract(pkg, dest, platform="linux")

    assert result.info.root_name == "MangaProof"
    assert result.entry_path == dest / "MangaProof" / "MangaProof"
    assert result.extracted >= 4

    entry = dest / "MangaProof" / "MangaProof"
    assert entry.is_file()
    assert entry.stat().st_mode & stat.S_IXUSR, "exec 位必须保留（需求 §52）"

    link = dest / "MangaProof" / "_internal" / "link.so"
    assert link.is_symlink(), "软链接必须还原（需求 §52）"
    assert os.readlink(link) == "libfoo.so"
    assert link.read_bytes() == b"LIB"

    lib = dest / "MangaProof" / "_internal" / "libfoo.so"
    assert not lib.stat().st_mode & stat.S_IXUSR, "普通文件不该凭空获得执行位"


def test_zip_restores_symlink_and_permission_bits(tmp_path: Path):
    """zipfile 自己不还原软链/权限位，安装器必须自己还原（调研报告 §5.3 R2）。"""
    pkg = sup.make_macos_package(tmp_path / "MangaProof-1.1.0-macos-arm64.zip")
    dest = tmp_path / "out"
    result = archive.safe_extract(pkg, dest, platform="macos")

    app = dest / "MangaProof.app"
    assert app.is_dir()
    entry = app / "Contents" / "MacOS" / "MangaProof"
    assert entry.is_file() and entry.stat().st_mode & stat.S_IXUSR
    assert result.entry_path == entry

    alias = app / "Contents" / "Frameworks" / "alias.txt"
    assert alias.is_symlink(), "zip 里的软链必须还原（否则 .app 直接崩）"
    assert os.readlink(alias) == "../Resources/real.txt"
    assert alias.read_bytes() == b"real\n"

    real = app / "Contents" / "Resources" / "real.txt"
    assert not real.stat().st_mode & 0o111, "普通文件必须保持 0o644"


def test_zip_is_extracted_into_parent_for_replacement(tmp_path: Path):
    """§54：解压到安装目录的**父目录**，归档顶层目录名就是新安装目录名。"""
    pkg = sup.make_linux_package(tmp_path / "pkg.tar.gz")
    parent = tmp_path / "example"
    parent.mkdir()
    archive.safe_extract(pkg, parent, platform="linux")
    assert (parent / "MangaProof" / "MangaProof").is_file()


def test_progress_and_item_callbacks_are_fed(tmp_path: Path):
    pkg = sup.make_linux_package(tmp_path / "pkg.tar.gz")
    seen_items: list[str] = []
    seen_progress: list[tuple[int, int]] = []
    archive.safe_extract(
        pkg, tmp_path / "out", platform="linux",
        on_item=seen_items.append,
        on_progress=lambda done, total: seen_progress.append((done, total)),
    )
    assert "MangaProof/MangaProof" in seen_items
    assert seen_progress, "解压必须上报计数（调研报告 §11.4）"
    assert seen_progress[-1][0] == seen_progress[-1][1] > 0


# --------------------------------------------------------------------------- #
# 攻击面：路径穿越 / 绝对路径 / 软链逃逸 / 设备文件
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entry_name",
    ["../evil.txt", "../../evil.txt", "MangaProof/../../evil.txt", "/tmp/evil.txt",
     "C:/Windows/evil.txt"],
)
def test_tar_rejects_traversal_and_absolute_paths(tmp_path: Path, entry_name: str):
    pkg = sup.write_tar_gz(
        tmp_path / "evil.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"#!/bin/sh\n"),
            sup.ArchiveEntry(entry_name, "file", 0o644, b"pwned"),
        ],
    )
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError) as exc:
        archive.safe_extract(pkg, dest, platform="linux")
    assert "需求 §52" in str(exc.value)
    assert not (tmp_path / "evil.txt").exists()
    assert not (dest / "evil.txt").exists()
    assert not Path("/tmp/evil.txt").exists()


@pytest.mark.parametrize("target", ["../../../../etc/passwd", "/etc/passwd",
                                    "../../../outside.txt", "MangaProof/../../../../escape"])
def test_tar_rejects_escaping_symlinks(tmp_path: Path, target: str):
    """链接目标解析后逃出**目标目录**必须被拒（解析是相对链接所在目录做的）。"""
    pkg = sup.write_tar_gz(
        tmp_path / "evil.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"#!/bin/sh\n"),
            sup.ArchiveEntry("MangaProof/_internal/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/_internal/evil", "sym", link=target),
        ],
    )
    dest = tmp_path / "out"
    with pytest.raises(ArchiveError):
        archive.safe_extract(pkg, dest, platform="linux")
    assert not (dest / "MangaProof" / "_internal" / "evil").exists(), "拒绝必须发生在落盘之前"
    assert not (tmp_path / "outside.txt").exists()


def test_tar_allows_links_that_stay_inside_the_root(tmp_path: Path):
    """链接指向包内其它位置是合法的（macOS 的 .app 交叉软链就靠这个）。"""
    pkg = sup.write_tar_gz(
        tmp_path / "ok.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"#!/bin/sh\n"),
            sup.ArchiveEntry("MangaProof/_internal/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/_internal/real.txt", "file", 0o644, b"data"),
            sup.ArchiveEntry("MangaProof/_internal/alias.txt", "sym", link="real.txt"),
            sup.ArchiveEntry("MangaProof/_internal/up.txt", "sym", link="../real2.txt"),
        ],
    )
    dest = tmp_path / "out"
    archive.safe_extract(pkg, dest, platform="linux")
    assert (dest / "MangaProof" / "_internal" / "alias.txt").read_bytes() == b"data"


def test_tar_rejects_device_files(tmp_path: Path):
    pkg = sup.write_tar_gz(
        tmp_path / "dev.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"#!/bin/sh\n"),
            sup.ArchiveEntry("MangaProof/zero", "chr", 0o666),
        ],
    )
    with pytest.raises(ArchiveError) as exc:
        archive.safe_extract(pkg, tmp_path / "out", platform="linux")
    assert "设备文件" in str(exc.value)


@pytest.mark.parametrize(
    "entry_name",
    ["../evil.txt", "/tmp/evil.txt", "MangaProof/../../evil.txt"],
)
def test_zip_rejects_traversal_and_absolute_paths(tmp_path: Path, entry_name: str):
    pkg = sup.write_zip(
        tmp_path / "evil.zip",
        [
            sup.ArchiveEntry("MangaProof.app/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/MacOS/MangaProof", "file", 0o755, b"x"),
            sup.ArchiveEntry(entry_name, "file", 0o644, b"pwned"),
        ],
    )
    with pytest.raises(ArchiveError):
        archive.safe_extract(pkg, tmp_path / "out", platform="macos")


def test_zip_rejects_escaping_symlink(tmp_path: Path):
    pkg = sup.write_zip(
        tmp_path / "evil.zip",
        [
            sup.ArchiveEntry("MangaProof.app/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/MacOS/MangaProof", "file", 0o755, b"x"),
            sup.ArchiveEntry("MangaProof.app/Contents/Frameworks/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/Frameworks/evil", "sym",
                             link="../../../../../etc/passwd"),
        ],
    )
    with pytest.raises(ArchiveError):
        archive.safe_extract(pkg, tmp_path / "out", platform="macos")


def test_zip_rejects_device_entry(tmp_path: Path):
    pkg = sup.write_zip(
        tmp_path / "dev.zip",
        [
            sup.ArchiveEntry("MangaProof.app/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/MacOS/MangaProof", "file", 0o755, b"x"),
            sup.ArchiveEntry("MangaProof.app/Contents/zero", "chr", 0o666),
        ],
    )
    with pytest.raises(ArchiveError) as exc:
        archive.safe_extract(pkg, tmp_path / "out", platform="macos")
    assert "设备文件" in str(exc.value)


def test_zip_skips_macos_resource_fork_dir(tmp_path: Path):
    pkg = sup.write_zip(
        tmp_path / "mac.zip",
        [
            sup.ArchiveEntry("__MACOSX/", "dir", 0o755),
            sup.ArchiveEntry("__MACOSX/._MangaProof.app", "file", 0o644, b"junk"),
            sup.ArchiveEntry("MangaProof.app/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/MacOS/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof.app/Contents/MacOS/MangaProof", "file", 0o755, b"x"),
        ],
    )
    dest = tmp_path / "out"
    result = archive.safe_extract(pkg, dest, platform="macos")
    assert not (dest / "__MACOSX").exists()
    assert result.skipped  # 上报为"跳过"而不是静默丢弃


# --------------------------------------------------------------------------- #
# 结构校验（需求 §51/§53）
# --------------------------------------------------------------------------- #


def test_missing_main_executable_is_rejected(tmp_path: Path):
    pkg = sup.write_tar_gz(
        tmp_path / "broken.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/README.txt", "file", 0o644, b"no main\n"),
        ],
    )
    with pytest.raises(ArchiveError) as exc:
        archive.inspect_package(pkg, "linux")
    assert "主程序" in str(exc.value)

    with pytest.raises(ArchiveError):
        archive.safe_extract(pkg, tmp_path / "out", platform="linux")
    assert not (tmp_path / "out" / "MangaProof").exists(), "结构异常不得进入替换流程"


def test_wrong_root_directory_is_rejected(tmp_path: Path):
    pkg = sup.write_tar_gz(
        tmp_path / "wrong.tar.gz",
        [
            sup.ArchiveEntry("OtherApp/", "dir", 0o755),
            sup.ArchiveEntry("OtherApp/MangaProof", "file", 0o755, b"x"),
        ],
    )
    with pytest.raises(ArchiveError) as exc:
        archive.inspect_package(pkg, "linux")
    assert "顶层结构异常" in str(exc.value)


def test_multiple_roots_are_rejected(tmp_path: Path):
    pkg = sup.write_tar_gz(
        tmp_path / "multi.tar.gz",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"x"),
            sup.ArchiveEntry("extra/", "dir", 0o755),
            sup.ArchiveEntry("extra/file.txt", "file", 0o644, b"y"),
        ],
    )
    with pytest.raises(ArchiveError):
        archive.inspect_package(pkg, "linux")


def test_format_must_match_platform(tmp_path: Path):
    pkg = sup.write_zip(
        tmp_path / "linux.zip",
        [
            sup.ArchiveEntry("MangaProof/", "dir", 0o755),
            sup.ArchiveEntry("MangaProof/MangaProof", "file", 0o755, b"x"),
        ],
    )
    with pytest.raises(ArchiveError) as exc:
        archive.inspect_package(pkg, "linux")
    assert "格式与平台不符" in str(exc.value)


def test_missing_installer_is_only_a_warning(tmp_path: Path):
    pkg = sup.make_linux_package(tmp_path / "pkg.tar.gz", with_installer=False)
    info = archive.inspect_package(pkg, "linux")
    assert info.has_installer is False  # 过渡版本仍可安装，只记警告


def test_corrupt_archive_is_rejected(tmp_path: Path):
    unknown = tmp_path / "mystery.bin"
    unknown.write_bytes(b"not an archive at all")
    with pytest.raises(ArchiveError) as exc:
        archive.archive_format(unknown)
    assert "无法识别" in str(exc.value)

    truncated = tmp_path / "corrupt.tar.gz"
    truncated.write_bytes(b"\x1f\x8b\x08broken-not-really-gzip")
    assert archive.archive_format(truncated) == "tar.gz"  # 魔数认得出来
    with pytest.raises(ArchiveError) as exc2:
        archive.inspect_package(truncated, "linux")
    assert "损坏" in str(exc2.value)


def test_normalize_member_name_helpers():
    assert archive.normalize_member_name("./MangaProof//MangaProof") == "MangaProof/MangaProof"
    assert archive.normalize_member_name("MangaProof\\MangaProof.exe") == "MangaProof/MangaProof.exe"
    with pytest.raises(ArchiveError):
        archive.normalize_member_name("a/../../b")
    assert archive.resolve_link_target(
        "MangaProof.app/Contents/Frameworks/x", "../Resources/y"
    ) == "MangaProof.app/Contents/Resources/y"
    with pytest.raises(ArchiveError):
        archive.resolve_link_target("MangaProof.app/Contents/x", "../../../../y")


def test_install_dir_relative_paths_differ_from_package_paths():
    """macOS 的 --install-dir 就是 .app：两条相对路径必须区分（需求 §38/§55）。"""
    assert archive.main_executable_rel("macos") == "MangaProof.app/Contents/MacOS/MangaProof"
    assert archive.install_main_rel("macos") == "Contents/MacOS/MangaProof"
    assert archive.install_main_rel("windows") == "MangaProof.exe"
    assert archive.install_main_rel("linux") == "MangaProof"
