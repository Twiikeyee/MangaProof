# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""Android 运行时依赖闭包 ↔ p4a recipe 归宿的守卫（CI 卡口的本地版）。

覆盖三件容易出事的事：

1. **uv.lock 里"显式请求的 extra"必须被展开**：pyproject 声明的是
   `httpx[socks]`，锁文件里写作 `{ name = "httpx", extra = ["socks"] }`。
   旧版闭包算法见到带 `extra` 的边就整条跳过，会把 httpx **本身**也丢掉，
   最终以"直接依赖未出现在闭包中"在 CI 上炸掉（症状离病因很远）。
2. **`ANDROID_SKIP` 的剪枝**：需求 §14 规定 Android 不打包 keyring，
   但 p4a 的 Python 上报 `sys.platform == "linux"`，keyring 的 Linux 专属
   依赖（SecretStorage → cryptography → cffi → pycparser）会被判为"适用"，
   不显式排除就会以 unknown 身份卡死 `--fail-on-unknown`。
3. **每个打包 token 都要有真实存在的本地 recipe 目录**：p4a 的
   `Recipe.name` 取自**目录名**，token 写错/漏建目录时，失败信息出现在
   几十分钟后的 buildozer 阶段；这里提前用文件系统断言挡住。

运行：uv run python -m pytest tests/test_android_lock_deps.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "android"))

import analyze_lock_deps as ald  # noqa: E402
from mangaproof.utils.platform import is_android_strict  # noqa: E402

_ON_ANDROID = is_android_strict()
pytestmark = pytest.mark.skipif(
    _ON_ANDROID, reason="依赖仓库内文件（uv.lock / packaging），Android 上无意义"
)

LOCK = ROOT / "uv.lock"
RECIPES_DIR = ROOT / "packaging" / "android" / "recipes"


@pytest.fixture(scope="module")
def result() -> dict:
    return ald.analyze(LOCK)


def test_requested_extra_pulls_in_the_package_itself(result):
    """httpx[socks]：包本身与 extra 依赖都要进闭包。"""
    names = {r["name"] for r in result["packages"]}
    assert "httpx" in names, "httpx 没进闭包——带 extra 的依赖边被整条跳过了"
    assert "socksio" in names, "被显式请求的 socks extra 没有展开"


def test_httpx_tree_is_categorised(result):
    """httpx 及其依赖必须全部有归宿（本地 recipe），且不得是 unknown。"""
    by_name = {r["name"]: r for r in result["packages"]}
    for name in ("httpx", "httpcore", "h11", "anyio", "certifi", "idna", "socksio"):
        assert name in by_name, f"{name} 不在闭包中"
        assert by_name[name]["android_action"] == "local-recipe", (
            f"{name} 的 Android 归宿应为 local-recipe，实际为 "
            f"{by_name[name]['android_action']}"
        )


def test_keyring_subtree_is_excluded(result):
    """keyring 及其整棵子树既不进闭包，也不进 unknown。"""
    names = {r["name"] for r in result["packages"]}
    skipped = set(result["excluded_android_skip"])
    assert "keyring" in skipped
    for name in ("keyring", "secretstorage", "cryptography", "cffi", "pycparser"):
        assert name not in names, f"{name} 不该被打进 APK（需求 §14）"
    # jaraco.* 在 uv.lock 里是连字符拼写，别写成 import 名
    for name in ("jaraco-classes", "jaraco-context", "jaraco-functools", "more-itertools"):
        assert name in skipped, f"{name} 应在 ANDROID_SKIP 中（实际：{sorted(skipped)}）"


def test_no_unknown_android_action(result):
    unknown = [r["name"] for r in result["packages"] if r["android_action"] == "unknown"]
    assert not unknown, f"以下依赖没有 Android 归宿：{unknown}"


def test_every_local_recipe_package_has_a_recipe_dir(result):
    """local-recipe 类目里的每个包都必须真的有 recipe 目录（名字=目录名）。"""
    for row in result["packages"]:
        if row["android_action"] != "local-recipe":
            continue
        recipe = RECIPES_DIR / row["name"] / "__init__.py"
        assert recipe.is_file(), f"缺少本地 recipe：{recipe.relative_to(ROOT)}"


def test_build_android_accepts_the_closure():
    """build_android.lock_requirements() 必须能吃下整份闭包（未知 token 会 SystemExit）。"""
    import build_android  # noqa: PLC0415

    tokens = build_android.lock_requirements()
    assert "httpx" in tokens
    assert "keyring" not in tokens, "keyring 不该进 p4a requirements（需求 §14）"


def test_recipe_pins_match_the_lock(result):
    """recipe 里钉的版本要与 uv.lock 一致，否则 APK 内的版本与锁文件悄悄分叉。"""
    import re  # noqa: PLC0415

    by_name = {r["name"]: r for r in result["packages"]}
    for row in result["packages"]:
        if row["android_action"] != "local-recipe":
            continue
        src = (RECIPES_DIR / row["name"] / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'version = "([^"]+)"', src)
        assert m, f"{row['name']} 的 recipe 没有钉版本"
        assert m.group(1) == str(row["version"]), (
            f"{row['name']} recipe 版本 {m.group(1)} != uv.lock {by_name[row['name']]['version']}"
        )
