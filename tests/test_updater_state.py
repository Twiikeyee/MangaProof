# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""状态文件与成功标记单测（需求 §58/§59/§64，调研报告 §5.4）。

重点：

- 阶段列表必须与需求 §78 的安装器状态机**逐字一致**；
- 状态落盘是原子的，且**绝不允许出现 CDK**（需求 §16/§77）；
- 成功标记按主程序侧的跨进程契约解析（``token``/``version``/``pid``/``ts``），
  且必须 **token + version 双匹配**才算成功。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from updater import state as st


def test_phase_list_matches_requirement_78():
    assert st.PHASES == (
        "INIT", "VALIDATE", "BACKUP_DATA", "VERIFY_PACKAGE", "RENAME_OLD",
        "EXTRACT", "RESTORE_DATA", "LAUNCH_NEW", "WAIT_SUCCESS", "SUCCESS",
        "CLEANUP", "ROLLBACK", "FAILED",
    )
    for phase in st.PHASES:
        assert st.label_for(phase) and st.label_for(phase) != phase, "每个阶段都要有中文文案"
    assert st.label_for("UNKNOWN_PHASE") == "UNKNOWN_PHASE"  # 未知阶段不得崩


def test_state_round_trip_and_atomic_write(tmp_path: Path):
    store = st.StateStore(tmp_path / "state" / st.STATE_FILE_NAME)
    state = st.UpdateState(
        install_dir="/opt/MangaProof", old_dir="/opt/MangaProof.old",
        package="/tmp/pkg.tar.gz", data_backup="/tmp/backup",
        success_marker="/tmp/marker.json", status_file=str(store.path),
        platform="linux", token="tok-1", version="1.1.0.alpha", parent_pid=4321,
    )
    store.transition(state, "VERIFY_PACKAGE", message="正在校验更新包…")

    payload = st.read_json(store.path)
    assert payload is not None
    assert payload["phase"] == "VERIFY_PACKAGE"
    assert payload["token"] == "tok-1" and payload["version"] == "1.1.0.alpha"
    assert payload["parent_pid"] == 4321
    assert payload["history"][-1]["phase"] == "VERIFY_PACKAGE"
    assert not list(tmp_path.rglob("*.tmp")), "原子写不得留下临时文件"

    restored = st.UpdateState.from_dict(payload)
    assert restored.install_dir == "/opt/MangaProof"
    assert restored.phase == "VERIFY_PACKAGE"
    # 未知字段被忽略（向前兼容）
    assert st.UpdateState.from_dict({**payload, "future_field": 1}).token == "tok-1"


def test_state_store_rejects_cdk(tmp_path: Path):
    store = st.StateStore(tmp_path / st.STATE_FILE_NAME)
    state = st.UpdateState(install_dir="/opt/MangaProof", token="tok")
    with pytest.raises(st.StateError):
        st.assert_no_secrets({**state.to_dict(), "mirrorchyan_cdk": "SECRET"})
    with pytest.raises(st.StateError):
        st.assert_no_secrets({"update": {"nested": {"CDK": "x"}}})
    assert not (tmp_path / st.STATE_FILE_NAME).exists(), "有敏感字段就不该写盘"


def test_state_store_disabled_is_noop():
    store = st.StateStore(None)
    assert store.enabled is False
    store.save(st.UpdateState(token="x"))  # 不抛错
    assert store.load() is None


def test_state_store_survives_unwritable_path(tmp_path: Path, monkeypatch):
    store = st.StateStore(tmp_path / "state.json")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(st, "atomic_write_json", boom)
    store.save(st.UpdateState(token="x"))  # 状态写不进去不该阻断安装


def test_marker_contract_matches_main_program(tmp_path: Path):
    """主程序侧契约：JSON 键为 token/version/pid/ts（跨进程对齐，不得改名）。"""
    marker = tmp_path / "success-marker.json"
    st.write_marker(marker, "tok-42", "1.1.0.alpha", pid=os.getpid())

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["token"] == "tok-42"
    assert payload["version"] == "1.1.0.alpha"
    assert payload["pid"] == os.getpid()
    assert isinstance(payload["ts"], (int, float))

    check = st.check_marker(marker, "tok-42", "1.1.0.alpha")
    assert check.valid is True
    assert check.reason


def test_marker_token_mismatch_is_not_success(tmp_path: Path):
    marker = tmp_path / "m.json"
    st.write_marker(marker, "other-token", "1.1.0.alpha", pid=1)
    check = st.check_marker(marker, "expected-token", "1.1.0.alpha")
    assert check.exists is True
    assert check.valid is False
    assert "token" in check.reason


def test_marker_version_mismatch_is_not_success(tmp_path: Path):
    marker = tmp_path / "m.json"
    st.write_marker(marker, "tok", "1.0.0", pid=1)
    check = st.check_marker(marker, "tok", "1.1.0.alpha")
    assert check.valid is False
    assert "版本" in check.reason


def test_marker_version_comparison_is_forgiving_about_prefix_and_case(tmp_path: Path):
    marker = tmp_path / "m.json"
    st.write_marker(marker, "tok", "v1.1.0.Alpha", pid=1)
    assert st.check_marker(marker, "tok", "1.1.0.alpha").valid is True
    assert st.normalize_version("  V1.1.0.ALPHA ") == "1.1.0.alpha"


def test_missing_and_malformed_marker(tmp_path: Path):
    marker = tmp_path / "none.json"
    check = st.check_marker(marker, "tok", "1.1.0.alpha")
    assert (check.exists, check.valid) == (False, False)

    marker.write_text("", encoding="utf-8")
    assert st.check_marker(marker, "tok", "1.1.0.alpha").valid is False

    # 容忍 key=value 文本（极端情况下主程序用一行 shell 写标记）
    marker.write_text("token=tok\nversion=1.1.0.alpha\n", encoding="utf-8")
    assert st.check_marker(marker, "tok", "1.1.0.alpha").valid is True


def test_empty_token_can_never_succeed(tmp_path: Path):
    marker = tmp_path / "m.json"
    st.write_marker(marker, "", "1.1.0.alpha", pid=1)
    assert st.check_marker(marker, "", "1.1.0.alpha").valid is False


def test_remove_marker(tmp_path: Path):
    marker = tmp_path / "m.json"
    st.write_marker(marker, "tok", "1.1.0.alpha")
    assert st.remove_marker(marker) is True
    assert st.remove_marker(marker) is False
    assert not marker.exists()


def test_update_state_to_dict_has_no_cdk_key_by_construction():
    payload = st.UpdateState().to_dict()
    assert "cdk" not in json.dumps(payload).lower()
    assert set(payload) >= {
        "phase", "token", "version", "install_dir", "old_dir", "package",
        "data_backup", "success_marker", "status_file", "platform",
        "parent_pid", "child_pid",
    }


def test_destructive_phases_cover_replacement_steps():
    assert {"RENAME_OLD", "EXTRACT", "RESTORE_DATA", "LAUNCH_NEW", "WAIT_SUCCESS"} <= set(
        st.DESTRUCTIVE_PHASES
    )
    assert "VERIFY_PACKAGE" not in st.DESTRUCTIVE_PHASES


def test_atomic_write_json_is_readable_and_complete(tmp_path: Path):
    target = tmp_path / "nested" / "data.json"
    st.atomic_write_json(target, {"a": 1, "b": "中文"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": "中文"}
    assert st.read_json(tmp_path / "missing.json") is None
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert st.read_json(tmp_path / "broken.json") is None
