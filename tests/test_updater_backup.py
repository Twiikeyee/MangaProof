# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""用户数据备份/恢复单测（需求 §47~§49、§56）。

白名单是唯一的依据，三条硬约束：

- **复制而非移动**：备份后原目录必须完好；
- **只碰白名单**：非白名单文件既不备份也不恢复；
- **幂等**：重复执行结果一致，且不留半截文件（临时名 + ``os.replace``）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mangaproof.config.user_data import USER_DATA_RULES

from updater import backup

import test_updater_support as sup  # noqa: E402


def test_backup_copies_whitelist_only(tmp_path: Path):
    install = sup.make_install_dir(tmp_path)
    backup_dir = tmp_path / "backup"
    items: list[str] = []
    progress: list[tuple[int, int]] = []

    report = backup.backup_user_data(
        install, backup_dir,
        on_item=items.append,
        on_progress=lambda done, total: progress.append((done, total)),
    )

    # 复制而非移动（需求 §49）
    assert (install / "settings.json").is_file()
    assert (install / "recent.json").is_file()
    assert (install / "logs" / "app.log").is_file()

    # 白名单内容原样出现在备份目录里
    assert (backup_dir / "settings.json").read_text(encoding="utf-8") == sup.USER_SETTINGS
    assert (backup_dir / "recent.json").read_text(encoding="utf-8") == sup.USER_RECENT
    assert (backup_dir / "logs" / "app.log").read_text(encoding="utf-8") == sup.USER_LOG

    # 非白名单文件不备份
    assert not (backup_dir / "not-user-data.bin").exists()
    assert not (backup_dir / "_internal").exists()

    # 目录本身也是一条条目（空 logs/ 也能被恢复）
    assert "logs" in report.items
    assert "settings.json" in report.items
    assert report.copied == len(report.items) == 4  # settings/recent/logs 目录/logs/app.log

    # UI 必须能显示具体文件（调研报告 §11.4）
    assert "logs/app.log" in items
    assert progress and progress[0] == (0, report.copied)
    assert progress[-1] == (report.copied, report.copied)


def test_backup_skips_missing_entries_without_failing(tmp_path: Path):
    install = sup.make_install_dir(tmp_path, with_user_data=False)
    report = backup.backup_user_data(install, tmp_path / "backup")
    assert report.copied == 0
    assert set(report.skipped) == {"settings.json", "recent.json", "logs"}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_backup_is_idempotent(tmp_path: Path):
    install = sup.make_install_dir(tmp_path)
    backup_dir = tmp_path / "backup"
    backup.backup_user_data(install, backup_dir)
    first = _snapshot(backup_dir)
    assert first, "备份目录不该是空的"
    backup.backup_user_data(install, backup_dir)
    assert _snapshot(backup_dir) == first
    assert not list(backup_dir.rglob("*.tmp")), "临时文件必须被 os.replace 掉"


def test_restore_recreates_data_and_is_idempotent(tmp_path: Path):
    install = sup.make_install_dir(tmp_path)
    backup_dir = tmp_path / "backup"
    backup.backup_user_data(install, backup_dir)

    # 模拟"新版解压后"的空安装目录（只剩程序，没有用户数据）
    fresh = tmp_path / "fresh"
    (fresh / "logs").mkdir(parents=True)
    (fresh / "settings.json").write_text('{"lang": "en"}', encoding="utf-8")
    (fresh / "unrelated.txt").write_text("keep me", encoding="utf-8")

    report = backup.restore_user_data(backup_dir, fresh)
    assert report.copied > 0
    assert (fresh / "settings.json").read_text(encoding="utf-8") == sup.USER_SETTINGS
    assert (fresh / "recent.json").read_text(encoding="utf-8") == sup.USER_RECENT
    assert (fresh / "logs" / "app.log").read_text(encoding="utf-8") == sup.USER_LOG
    assert (fresh / "unrelated.txt").read_text(encoding="utf-8") == "keep me"
    assert not list(fresh.rglob("*.tmp"))

    # 再恢复一次：结果完全相同（幂等，需求 §56 + 调研报告 §5.4）
    backup.restore_user_data(backup_dir, fresh)
    assert (fresh / "settings.json").read_text(encoding="utf-8") == sup.USER_SETTINGS
    assert not list(fresh.rglob("*.tmp"))


def test_restore_from_empty_backup_keeps_install_dir_usable(tmp_path: Path):
    install = tmp_path / "MangaProof"
    install.mkdir()
    (install / "MangaProof").write_text("bin", encoding="utf-8")
    report = backup.restore_user_data(tmp_path / "empty-backup", install)
    assert report.copied == 0
    assert (install / "MangaProof").is_file()


def test_backup_dir_inside_install_dir_is_rejected(tmp_path: Path):
    install = sup.make_install_dir(tmp_path)
    with pytest.raises(backup.BackupError):
        backup.backup_user_data(install, install / "backup-inside")


def test_plan_copy_matches_whitelist_order(tmp_path: Path):
    install = sup.make_install_dir(tmp_path)
    entries, missing = backup.plan_copy(install, tmp_path / "b", rules=USER_DATA_RULES)
    assert not missing
    assert [e.rel for e in entries[:3]] == ["settings.json", "recent.json", "logs"]
    assert "logs/app.log" in [e.rel for e in entries]


def test_remove_backup_dir(tmp_path: Path):
    backup_dir = tmp_path / "backup"
    (backup_dir / "sub").mkdir(parents=True)
    (backup_dir / "sub" / "x").write_text("x", encoding="utf-8")
    assert backup.remove_backup_dir(backup_dir) is True
    assert not backup_dir.exists()
    assert backup.remove_backup_dir(backup_dir) is False  # 不存在也不报错


def test_whitelist_summary_mentions_all_rules():
    text = backup.whitelist_summary()
    for rule in USER_DATA_RULES:
        assert rule["path"] in text
