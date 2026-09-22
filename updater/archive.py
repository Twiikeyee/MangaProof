# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""更新包格式、结构校验与安全解压（需求 §50~§52，调研报告 §5.2/§5.3）。

三种格式对应（需求 §50）：

===========  ==========  ================================================
平台         格式        解压方式
===========  ==========  ================================================
Windows      ZIP         自实现（符号链接 + 权限位，见下）
Linux        TAR.GZ      ``tarfile.extractall(..., filter="data")``
macOS        ZIP         自实现（符号链接 + 权限位，见下）
===========  ==========  ================================================

两个必须按调研报告做的坑：

1. **tar.gz 必须显式传 ``filter="data"``**（§5.2）：Python 3.12 的默认过滤器
   仍是 ``fully_trusted``（只发 DeprecationWarning）。``data`` 过滤器同时满足
   "防路径穿越/设备文件/越界链接"与"**保留 exec 位**"（它保留 owner-exec，
   清掉 setuid/setgid/组与其他写位）。
2. **ZIP 不能直接用 ``zipfile.extractall``**（§5.3，最高风险）：Python 的 zipfile
   **既不还原符号链接、也不还原 Unix 权限位**（``external_attr`` 被完全忽略）。
   PyInstaller 的 ``.app`` 依赖 ``Contents/Frameworks`` ↔ ``Contents/Resources``
   交叉软链，直接 extractall 会得到"内容为链接路径的普通文件"→ codesign 失败 /
   bundle 格式歧义 / 启动即崩。因此这里自己解：按
   ``(zi.external_attr >> 16) & 0xFFFF`` 取 mode，``stat.S_IFLNK`` 时
   ``os.symlink(zf.read(zi).decode())``，普通文件 ``os.chmod(mode)``。

安全检查（需求 §52）：``../``、绝对路径（含 ``C:`` 盘符）、路径穿越、异常软链接
（链接目标解析后逃出解压根目录）、目标目录逃逸、设备/FIFO/socket 条目，以及
"解压后主程序是否存在"。**先全量扫描再解压**：任何一条不合规都在动磁盘之前失败。

顺带一提：更新包结构校验的"顶层目录名 / 主程序相对路径"常量也放在本模块，
``verify.py`` 直接复用——这样 ``archive`` 保持零依赖，不存在循环导入。
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

log = logging.getLogger("mangaproof.updater.archive")

ItemCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]

#: 支持的平台标识（``--platform``）
PLATFORMS: tuple[str, ...] = ("windows", "linux", "macos")

#: 更新包顶层目录名（需求 §51 + 调研报告 §11.3）
APP_ROOT_NAME: dict[str, str] = {
    "windows": "MangaProof",
    "linux": "MangaProof",
    "macos": "MangaProof.app",
}

#: 主程序在**包内**的相对路径（相对解压父目录；需求 §51）
MAIN_EXECUTABLE_REL: dict[str, str] = {
    "windows": "MangaProof/MangaProof.exe",
    "linux": "MangaProof/MangaProof",
    "macos": "MangaProof.app/Contents/MacOS/MangaProof",
}

#: 安装器在**包内**的相对路径（调研报告 §11.3：与主程序可执行文件同级）
INSTALLER_REL: dict[str, str] = {
    "windows": "MangaProof/MangaProof-update-installer.exe",
    "linux": "MangaProof/MangaProof-update-installer",
    "macos": "MangaProof.app/Contents/MacOS/MangaProof-update-installer",
}

#: 主程序在**安装目录内**的相对路径。
#: 注意与包内路径的区别：macOS 的 ``--install-dir`` **就是** ``MangaProof.app``
#: （需求 §38/§55，主程序侧契约同此），所以安装目录内没有 ``MangaProof.app/`` 前缀。
INSTALL_MAIN_REL: dict[str, str] = {
    "windows": "MangaProof.exe",
    "linux": "MangaProof",
    "macos": "Contents/MacOS/MangaProof",
}

#: 安装器在**安装目录内**的相对路径（§11.3：与主程序可执行文件同级同目录）
INSTALL_INSTALLER_REL: dict[str, str] = {
    "windows": "MangaProof-update-installer.exe",
    "linux": "MangaProof-update-installer",
    "macos": "Contents/MacOS/MangaProof-update-installer",
}

#: 平台对应的更新包格式（需求 §50）
PACKAGE_FORMAT: dict[str, str] = {"windows": "zip", "linux": "tar.gz", "macos": "zip"}

#: 归档里允许存在但**不解压**的顶层目录（macOS 的 zip 会带资源分叉）
SKIPPED_ROOTS: frozenset[str] = frozenset({"__MACOSX"})

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_ZIP_MAGIC = b"PK\x03\x04"
_GZIP_MAGIC = b"\x1f\x8b"


class ArchiveError(Exception):
    """更新包不可用（格式/结构/解压失败）——需求 §62：直接进入失败流程。"""


@dataclass(frozen=True)
class Member:
    """归档内的一个条目（路径已规范化成 posix 相对路径）。"""

    name: str
    kind: str  # dir | file | link | hardlink
    mode: int = 0
    link_target: str = ""
    size: int = 0

    @property
    def is_dir(self) -> bool:
        return self.kind == "dir"

    @property
    def is_link(self) -> bool:
        return self.kind in ("link", "hardlink")


@dataclass(frozen=True)
class ArchiveInfo:
    """结构校验结果（§51/§53 的"压缩结构正确 + 主程序存在"）。"""

    fmt: str
    root_name: str
    main_rel: str
    installer_rel: str
    member_count: int
    total_bytes: int
    has_installer: bool

    @property
    def root_dir_name(self) -> str:
        """顶层目录的**末级名**（Windows/Linux 就是 ``MangaProof``）。"""
        return PurePosixPath(self.root_name).name


@dataclass(frozen=True)
class ExtractResult:
    """解压结果。"""

    info: ArchiveInfo
    extracted: int
    skipped: tuple[str, ...]
    entry_path: Path


# --------------------------------------------------------------------------- #
# 路径与成员校验
# --------------------------------------------------------------------------- #


def normalize_member_name(name: str) -> str:
    """规范化归档内路径；绝对路径 / 盘符 / ``..`` 一律拒绝（需求 §52）。"""
    text = str(name).replace("\\", "/").strip()
    if not text:
        raise ArchiveError("更新包中存在空路径条目")
    if "\x00" in text:
        raise ArchiveError("更新包中存在含 NUL 的非法路径条目")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text):
        raise ArchiveError(f"更新包中存在绝对路径条目：{name!r}（需求 §52）")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ArchiveError(f"更新包中存在路径穿越条目：{name!r}（需求 §52）")
        parts.append(part)
    if not parts:
        raise ArchiveError(f"更新包中存在无效路径条目：{name!r}")
    return "/".join(parts)


def resolve_link_target(link_rel: str, target: str) -> str:
    """把软链接目标解析成归档内路径；逃出顶层目录即拒绝（需求 §52）。

    ``link_rel`` 是链接自身的归档内路径（posix），``target`` 是链接内容
    （相对路径）。解析是**纯词法**的（``posixpath.normpath``），不碰文件系统：
    解压前就要判定，不能等链接建出来再发现它指向 ``/etc``。
    """
    text = str(target).replace("\\", "/")
    if not text or "\x00" in text:
        raise ArchiveError(f"更新包中 {link_rel!r} 的软链接目标非法：{target!r}")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text):
        raise ArchiveError(
            f"更新包中 {link_rel!r} 的软链接指向绝对路径：{target!r}（需求 §52）"
        )
    base = posixpath.dirname(link_rel)
    resolved = posixpath.normpath(posixpath.join(base, text))
    if resolved in ("", ".", "..") or resolved.startswith("../") or resolved.startswith("/"):
        raise ArchiveError(
            f"更新包中 {link_rel!r} 的软链接逃出目标目录：{target!r}（需求 §52）"
        )
    return resolved


def _contained(path: Path, root: Path) -> bool:
    """``path`` 是否在 ``root`` 之内（按 realpath 比较，防软链绕过）。"""
    try:
        real_root = os.path.realpath(root)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_path, real_root]) == real_root
    except (OSError, ValueError):
        return False


def archive_format(path: Path) -> str:
    """判定归档格式：优先看魔数，其次看后缀；都不认识就报错。"""
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError as exc:
        raise ArchiveError(f"更新包无法读取：{path}（{exc}）") from exc
    if head.startswith(_ZIP_MAGIC):
        return "zip"
    if head.startswith(_GZIP_MAGIC):
        return "tar.gz"
    name = Path(path).name.lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    raise ArchiveError(f"无法识别的更新包格式：{Path(path).name}（需求 §50）")


def expected_format(platform: str) -> str:
    try:
        return PACKAGE_FORMAT[platform]
    except KeyError as exc:
        raise ArchiveError(f"不支持的平台标识：{platform!r}") from exc


def main_executable_rel(platform: str) -> str:
    """主程序在**包内**的相对路径（相对解压父目录）。"""
    try:
        return MAIN_EXECUTABLE_REL[platform]
    except KeyError as exc:
        raise ArchiveError(f"不支持的平台标识：{platform!r}") from exc


def install_main_rel(platform: str) -> str:
    """主程序在**安装目录内**的相对路径（相对 ``--install-dir``）。"""
    try:
        return INSTALL_MAIN_REL[platform]
    except KeyError as exc:
        raise ArchiveError(f"不支持的平台标识：{platform!r}") from exc


def app_root_name(platform: str) -> str:
    try:
        return APP_ROOT_NAME[platform]
    except KeyError as exc:
        raise ArchiveError(f"不支持的平台标识：{platform!r}") from exc


# --------------------------------------------------------------------------- #
# 成员枚举（tar / zip 两条路径）
# --------------------------------------------------------------------------- #


def _member_from_zipinfo(
    zf: zipfile.ZipFile, zi: zipfile.ZipInfo
) -> Member:
    """把 ZipInfo 变成 Member：**权限位与软链必须自己从 external_attr 还原**。"""
    if zi.flag_bits & 0x1:
        raise ArchiveError(f"更新包中存在加密条目：{zi.filename!r}")
    name = normalize_member_name(zi.filename)
    raw_mode = (zi.external_attr >> 16) & 0xFFFF
    kind = "file"
    if stat.S_ISLNK(raw_mode):
        kind = "link"
    elif stat.S_ISDIR(raw_mode) or zi.is_dir():
        kind = "dir"
    elif stat.S_ISCHR(raw_mode) or stat.S_ISBLK(raw_mode):
        raise ArchiveError(f"更新包中存在设备文件条目：{zi.filename!r}（需求 §52）")
    elif stat.S_ISFIFO(raw_mode) or stat.S_ISSOCK(raw_mode):
        raise ArchiveError(f"更新包中存在 FIFO/socket 条目：{zi.filename!r}（需求 §52）")
    elif raw_mode and not stat.S_ISREG(raw_mode):
        raise ArchiveError(f"更新包中存在非常规条目：{zi.filename!r}（需求 §52）")
    link_target = ""
    if kind == "link":
        try:
            link_target = zf.read(zi).decode("utf-8", "surrogateescape")
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"更新包中软链接 {zi.filename!r} 读取失败：{exc}") from exc
        resolve_link_target(name, link_target)
    if kind == "dir":
        mode = (raw_mode & 0o777) or 0o755
    elif kind == "link":
        mode = 0o777
    else:
        # 没有权限信息的 zip（Windows 打包）按 0o644 处理
        mode = (raw_mode & 0o777) or 0o644
    return Member(
        name=name, kind=kind, mode=mode, link_target=link_target, size=int(zi.file_size)
    )


def _member_from_tarinfo(ti: tarfile.TarInfo) -> Member:
    name = normalize_member_name(ti.name)
    mode = int(ti.mode or 0) & 0o7777
    if ti.isdir():
        return Member(name=name, kind="dir", mode=mode or 0o755, size=0)
    if ti.issym():
        resolve_link_target(name, ti.linkname)
        return Member(name=name, kind="link", mode=0o777, link_target=ti.linkname, size=0)
    if ti.islnk():
        target = normalize_member_name(ti.linkname)
        return Member(name=name, kind="hardlink", mode=mode, link_target=target, size=0)
    if ti.ischr() or ti.isblk() or ti.isfifo() or ti.isdev():
        raise ArchiveError(f"更新包中存在设备文件条目：{ti.name!r}（需求 §52）")
    if not ti.isreg():
        raise ArchiveError(f"更新包中存在非常规条目：{ti.name!r}（需求 §52）")
    return Member(name=name, kind="file", mode=mode or 0o644, size=int(ti.size or 0))


def iter_members(path: Path) -> list[Member]:
    """枚举归档成员并逐个做安全检查（不落盘）。"""
    fmt = archive_format(path)
    if fmt == "zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return [_member_from_zipinfo(zf, zi) for zi in zf.infolist()]
        except zipfile.BadZipFile as exc:
            raise ArchiveError(f"更新包已损坏（zip）：{Path(path).name}（{exc}）") from exc
        except OSError as exc:
            raise ArchiveError(f"更新包无法读取：{Path(path).name}（{exc}）") from exc
    try:
        with tarfile.open(path, "r:*") as tf:
            return [_member_from_tarinfo(ti) for ti in tf.getmembers()]
    except tarfile.TarError as exc:
        raise ArchiveError(f"更新包已损坏（tar）：{Path(path).name}（{exc}）") from exc
    except OSError as exc:
        raise ArchiveError(f"更新包无法读取：{Path(path).name}（{exc}）") from exc


def validate_members(members: Sequence[Member], platform: str) -> ArchiveInfo:
    """结构校验（§51/§53）：单一顶层目录 + 顶层目录名正确 + 主程序存在。"""
    fmt = expected_format(platform)
    root = app_root_name(platform)
    main_rel = main_executable_rel(platform)
    installer_rel = INSTALLER_REL.get(platform, "")

    materialized = [m for m in members if m.name.split("/")[0] not in SKIPPED_ROOTS]
    if not materialized:
        raise ArchiveError("更新包是空的：没有任何可解压条目")

    roots = {m.name.split("/")[0] for m in materialized}
    if roots != {root}:
        raise ArchiveError(
            "更新包顶层结构异常：期望单一顶层目录 "
            f"{root!r}，实际 {sorted(roots)!r}（需求 §51；不进入替换流程）"
        )

    files = {m.name for m in materialized if m.kind == "file"}
    if main_rel not in files:
        raise ArchiveError(
            f"更新包结构异常：缺少主程序 {main_rel!r}（需求 §51/§53；不进入替换流程）"
        )

    has_installer = installer_rel in files
    if not has_installer:
        # 调研报告 §11.3 要求安装器与主程序同级随包发布；这里是**警告**而不是
        # 拒绝：拒绝会让"安装器尚未进包"的过渡版本完全无法更新。
        log.warning("更新包内未找到安装器 %s（调研报告 §11.3 建议随包分发）", installer_rel)

    return ArchiveInfo(
        fmt=fmt,
        root_name=root,
        main_rel=main_rel,
        installer_rel=installer_rel,
        member_count=len(materialized),
        total_bytes=sum(m.size for m in materialized if m.kind == "file"),
        has_installer=has_installer,
    )


def _scan(path: Path, platform: str) -> tuple[ArchiveInfo, list[Member]]:
    """一次扫描拿到"成员表 + 结构校验结果"（调用方复用，避免重复读包）。"""
    if not Path(path).is_file():
        raise ArchiveError(f"更新包不存在：{path}（需求 §53）")
    fmt = archive_format(path)
    want = expected_format(platform)
    if fmt != want:
        raise ArchiveError(
            f"更新包格式与平台不符：{Path(path).name} 是 {fmt}，{platform} 需要 {want}（需求 §50）"
        )
    members = iter_members(path)
    info = validate_members(members, platform)
    return info, members


def inspect_package(path: Path, platform: str) -> ArchiveInfo:
    """只做"格式 + 结构"校验，不解压（§53 的第二步）。"""
    info, _members = _scan(Path(path), platform)
    return info


# --------------------------------------------------------------------------- #
# 解压
# --------------------------------------------------------------------------- #


def _report(on_item: ItemCallback | None, on_progress: ProgressCallback | None,
            done: int, total: int, name: str) -> None:
    if on_item is not None:
        on_item(name)
    if on_progress is not None:
        on_progress(done, total)


def _make_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _extract_zip(
    archive_path: Path,
    dest_dir: Path,
    members: Sequence[Member],
    *,
    on_item: ItemCallback | None,
    on_progress: ProgressCallback | None,
) -> tuple[int, list[str]]:
    """自实现 zip 解压：还原权限位与软链接，并防目标目录逃逸。"""
    skipped: list[str] = []
    # 目录先建、文件随后（避免权限不足的目录顺序问题）
    ordered = sorted(members, key=lambda m: 0 if m.is_dir else 1)
    with zipfile.ZipFile(archive_path) as zf:
        by_name: dict[str, zipfile.ZipInfo] = {}
        for zi in zf.infolist():
            try:
                by_name[normalize_member_name(zi.filename)] = zi
            except ArchiveError:
                continue  # 非法条目在 iter_members 阶段已经报错过
        total = len([m for m in members if m.name.split("/")[0] not in SKIPPED_ROOTS])
        done = 0
        for member in ordered:
            if member.name.split("/")[0] in SKIPPED_ROOTS:
                skipped.append(member.name)
                continue
            target = dest_dir.joinpath(*PurePosixPath(member.name).parts)
            if not _contained(target, dest_dir):
                raise ArchiveError(
                    f"解压目标逃出目标目录：{member.name!r}（需求 §52）"
                )
            raw = by_name.get(member.name)
            if raw is None:  # 理论上不会发生；宁可报错也不静默跳过
                raise ArchiveError(f"更新包条目丢失：{member.name!r}")
            if member.is_dir:
                target.mkdir(parents=True, exist_ok=True)
                _chmod(target, member.mode)
            elif member.kind == "link":
                _make_parent(target)
                _replace_link(raw, zf, target, member, dest_dir)
            else:
                _make_parent(target)
                target.unlink(missing_ok=True)
                with zf.open(raw) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                _chmod(target, member.mode)
                _copy_mtime(target, raw)
            done += 1
            _report(on_item, on_progress, done, total, member.name)
    return done, skipped


def _replace_link(
    raw: zipfile.ZipInfo,
    zf: zipfile.ZipFile,
    target: Path,
    member: Member,
    dest_dir: Path,
) -> None:
    """创建软链接（zip 的 symlink 内容就是链接目标）。"""
    link_text = member.link_target or zf.read(raw).decode("utf-8", "surrogateescape")
    # 词法检查已在 iter_members 做过；这里再按真实路径兜一次（防已存在的软链）
    resolved = resolve_link_target(member.name, link_text)
    lexical = dest_dir.joinpath(*PurePosixPath(resolved).parts)
    if not _contained(lexical, dest_dir):
        raise ArchiveError(f"软链接逃出目标目录：{member.name!r} → {link_text!r}（需求 §52）")
    if target.is_symlink() or target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    os.symlink(link_text, target)
    if not _contained(target, dest_dir):
        try:
            target.unlink()
        finally:
            raise ArchiveError(
                f"软链接解析后逃出目标目录：{member.name!r} → {link_text!r}（需求 §52）"
            )


def _chmod(path: Path, mode: int) -> None:
    if os.name == "nt":
        return  # Windows 没有 POSIX 权限位；执行与否由扩展名决定
    try:
        os.chmod(path, mode & 0o777)
    except OSError as exc:  # 权限位失败不该让安装整体失败
        log.warning("设置权限失败：%s（mode=%o，%s）", path, mode, exc)


def _copy_mtime(path: Path, raw: zipfile.ZipInfo) -> None:
    try:
        import time as _time

        stamp = _time.mktime(tuple(raw.date_time) + (0, 0, -1))
        os.utime(path, (stamp, stamp))
    except (OSError, ValueError, OverflowError):
        pass


def _extract_tar(
    archive_path: Path,
    dest_dir: Path,
    members: Sequence[Member],
    *,
    on_item: ItemCallback | None,
    on_progress: ProgressCallback | None,
) -> tuple[int, list[str]]:
    """tar.gz 解压：逐条 ``extract(filter="data")``（显式传参，见模块文档）。"""
    skipped: list[str] = []
    # 硬链接放到最后：tar 里的硬链接要求目标已存在
    ordered = sorted(
        members,
        key=lambda m: {"dir": 0, "file": 1, "link": 2, "hardlink": 3}.get(m.kind, 4),
    )
    total = len([m for m in members if m.name.split("/")[0] not in SKIPPED_ROOTS])
    done = 0
    with tarfile.open(archive_path, "r:*") as tf:
        by_name: dict[str, tarfile.TarInfo] = {}
        for ti in tf.getmembers():
            try:
                by_name[normalize_member_name(ti.name)] = ti
            except ArchiveError:
                continue
        for member in ordered:
            if member.name.split("/")[0] in SKIPPED_ROOTS:
                skipped.append(member.name)
                continue
            raw = by_name.get(member.name)
            if raw is None:
                raise ArchiveError(f"更新包条目丢失：{member.name!r}")
            if member.kind == "link":
                # 软链接自身也要落在目标目录内（data 过滤器会再校验一次）
                resolve_link_target(member.name, member.link_target)
            if member.kind == "hardlink":
                target = dest_dir.joinpath(*PurePosixPath(member.link_target).parts)
                if not _contained(target, dest_dir):
                    raise ArchiveError(
                        f"硬链接逃出目标目录：{member.name!r} → {member.link_target!r}（需求 §52）"
                    )
            try:
                # 关键：显式 filter="data"（Python 3.12 默认不是它，调研报告 §5.2）
                tf.extract(raw, path=str(dest_dir), filter="data", numeric_owner=False)
            except (tarfile.TarError, OSError, ValueError) as exc:
                raise ArchiveError(
                    f"解压失败：{member.name!r}（{exc}）（需求 §62）"
                ) from exc
            done += 1
            _report(on_item, on_progress, done, total, member.name)
    return done, skipped


def safe_extract(
    archive_path: Path,
    dest_dir: Path,
    *,
    platform: str,
    on_item: ItemCallback | None = None,
    on_progress: ProgressCallback | None = None,
    require_main: bool = True,
    repair_exec: bool = True,
) -> ExtractResult:
    """安全解压更新包到 ``dest_dir``，并断言主程序已就位（需求 §52/§54）。

    ``dest_dir`` 应当是安装目录的**父目录**（§54：新版本展开到同一父目录），
    归档内的顶层目录名就是新安装目录名。
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    info, members = _scan(Path(archive_path), platform)
    if info.fmt == "zip":
        extracted, skipped = _extract_zip(
            Path(archive_path), dest, members, on_item=on_item, on_progress=on_progress
        )
    else:
        extracted, skipped = _extract_tar(
            Path(archive_path), dest, members, on_item=on_item, on_progress=on_progress
        )
    entry = find_main_in_extract(dest, platform)
    if require_main and entry is None:
        raise ArchiveError(
            f"解压后未找到主程序：{dest / info.main_rel}（需求 §52）"
        )
    if entry is not None and repair_exec and platform in ("linux", "macos"):
        _ensure_executable(entry)
    return ExtractResult(
        info=info,
        extracted=extracted,
        skipped=tuple(skipped),
        entry_path=entry if entry is not None else dest / info.main_rel,
    )


def _ensure_executable(path: Path) -> None:
    """需求 §41：启动前确认可执行，必要时 ``chmod +x``。"""
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    want = mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if mode & stat.S_IXUSR:
        return
    try:
        os.chmod(path, want & 0o777)
        log.warning("主程序缺少可执行位，已 chmod +x：%s", path)
    except OSError as exc:
        log.warning("chmod +x 失败：%s（%s）", path, exc)


def find_main_in_extract(dest_dir: Path, platform: str) -> Path | None:
    """在**解压结果**（解压父目录）里找主程序；找不到返回 ``None``。

    用包内路径（macOS 含 ``MangaProof.app/`` 前缀）。
    """
    rel = main_executable_rel(platform)
    candidate = Path(dest_dir).joinpath(*PurePosixPath(rel).parts)
    if candidate.is_file():
        return candidate
    return None


def find_main_executable(install_dir: Path, platform: str) -> Path | None:
    """在**安装目录**里找主程序；找不到返回 ``None``。

    用安装目录内路径（macOS 的 ``--install-dir`` 就是 ``MangaProof.app``，
    因此是 ``Contents/MacOS/MangaProof``）。
    """
    rel = install_main_rel(platform)
    candidate = Path(install_dir).joinpath(*PurePosixPath(rel).parts)
    if candidate.is_file():
        return candidate
    return None


def extracted_root(dest_dir: Path, platform: str) -> Path:
    """解压后新版本的顶层目录（安装目录内容所在处）。"""
    return Path(dest_dir) / app_root_name(platform)


__all__ = [
    "APP_ROOT_NAME",
    "ArchiveError",
    "ArchiveInfo",
    "ExtractResult",
    "INSTALLER_REL",
    "INSTALL_INSTALLER_REL",
    "INSTALL_MAIN_REL",
    "MAIN_EXECUTABLE_REL",
    "Member",
    "PACKAGE_FORMAT",
    "PLATFORMS",
    "SKIPPED_ROOTS",
    "app_root_name",
    "archive_format",
    "expected_format",
    "extracted_root",
    "find_main_executable",
    "find_main_in_extract",
    "inspect_package",
    "install_main_rel",
    "iter_members",
    "main_executable_rel",
    "normalize_member_name",
    "resolve_link_target",
    "safe_extract",
    "validate_members",
]
