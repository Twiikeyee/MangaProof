#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""解析 uv.lock，输出 Android 打包所需的『运行时依赖闭包』。

为什么要这个脚本
----------------
`pyproject.toml` 只有 4 个直接依赖，而 Android 侧要为**整棵传递依赖树**准备
p4a recipe（psd-tools 的 attrs/typing-extensions、reportlab 的
charset-normalizer、以及共同依赖的 Pillow/numpy…）。`uv.lock` 里才有完整的
解析结果与依赖边。

三条规则（也是防「依赖组污染」的机制）
--------------------------------------
1. **只从根包**（`source = { editable = "." }`，即本项目）的 `dependencies`
   出发遍历——那是 uv 写进 lock 的**运行时**依赖（对应
   `[project].dependencies`）；`dev-dependencies = { dev = [...], speedup = [...] }`
   是**依赖组**，从它们出发的包永远不可达。
2. 带 `extra` 的依赖边，只有该 extra 被显式请求时才跟随（本项目不请求任何
   extra，因此 `psd-tools[composite]` 的 aggdraw/scipy 自动排除）。
3. **硬校验** `运行时闭包 ∩ 组专属包 == ∅`。注意区分「组专属」与「两边都要」：
   例如 `typing-extensions` 既是 psd-tools 的运行时依赖、又被 dev 组间接引入，
   它**必须打包**——判据是「组可达集 − 运行时闭包」，不能简单地按组去重。

因此：**本机装了哪个依赖组（包括为性能调优装的 speedup）都不影响结果**。

用法
----
    python scripts/android/analyze_lock_deps.py                  # 人类可读表格
    python scripts/android/analyze_lock_deps.py --json           # 供 CI 消费
    python scripts/android/analyze_lock_deps.py --fail-on-unknown # CI 卡口
    python scripts/android/analyze_lock_deps.py --lock other/uv.lock
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 已核实的 Android 打包归宿（新增依赖若不在表内即视为 unknown，CI 应失败）
QT_WHEEL = {"pyside6", "pyside6-essentials", "pyside6-addons", "shiboken6"}
P4A_OFFICIAL = {"numpy", "pillow", "reportlab"}          # p4a develop 自带 recipe（版本可能偏旧）
LOCAL_RECIPE = {"attrs", "typing-extensions", "charset-normalizer", "psd-tools"}
NATIVE = QT_WHEEL | {"numpy", "pillow"}


def find_root(packages: list[dict]) -> dict:
    """定位根项目（editable / virtual 指向 "."）。"""
    for pkg in packages:
        source = pkg.get("source", {})
        if source.get("editable") == "." or source.get("virtual") == ".":
            return pkg
    raise SystemExit("[deps] uv.lock 中找不到根项目（source.editable/virtual == '.'）")


def reachable(packages: dict[str, dict], starts, *, skip_extras: bool = True) -> set[str]:
    """从 starts 出发的依赖闭包；skip_extras 时不跟随未被请求的 extra 边。"""
    seen: set[str] = set()
    queue = deque(starts)
    while queue:
        name = queue.popleft()
        if name in seen:
            continue
        seen.add(name)
        for dep in packages.get(name, {}).get("dependencies", []):
            if skip_extras and (dep.get("extra") or "extra ==" in (dep.get("marker") or "")):
                continue
            queue.append(dep["name"])
    return seen


def android_action(name: str) -> str:
    if name in QT_WHEEL:
        return "qt-wheel"
    if name in P4A_OFFICIAL:
        return "p4a-official"
    if name in LOCAL_RECIPE:
        return "local-recipe"
    return "unknown"


def analyze(lock_path: Path) -> dict:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    root = find_root(lock["package"])

    direct = [d["name"] for d in root.get("dependencies", [])]
    runtime = reachable(packages, direct) | {root["name"]}

    group_reach: set[str] = set()
    group_of: dict[str, str] = {}
    for group_name, deps in (root.get("dev-dependencies") or {}).items():
        for name in reachable(packages, [d["name"] for d in deps]):
            group_reach.add(name)
            group_of.setdefault(name, group_name)
    group_only = sorted(group_reach - runtime)
    overlap = sorted(group_reach & runtime)

    # 硬校验：闭包绝不能混入「组专属包」
    leaked = sorted(runtime & set(group_only))
    if leaked:
        raise SystemExit(f"[deps] 运行时闭包混入组专属包：{leaked}")
    missing = [d for d in direct if d not in runtime]
    if missing:
        raise SystemExit(f"[deps] 直接依赖未出现在闭包中：{missing}")

    rows = [
        {
            "name": name,
            "version": packages[name].get("version"),
            "relation": "direct" if name in direct else "transitive",
            "pure_python": name not in NATIVE,
            "android_action": android_action(name),
        }
        for name in sorted(runtime)
        if name != root["name"]
    ]
    return {
        "root": {"name": root["name"], "version": root.get("version")},
        "packages": rows,
        "excluded_group_only": group_only,
        "excluded_group_of": {n: group_of[n] for n in group_only},
        "group_overlap_must_package": overlap,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 uv.lock 得到 Android 运行时依赖闭包")
    parser.add_argument("--lock", type=Path, default=REPO_ROOT / "uv.lock")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供 CI 消费）")
    parser.add_argument("--fail-on-unknown", action="store_true",
                        help="存在 android_action=unknown 的包时以非零码退出（CI 卡口）")
    args = parser.parse_args()

    result = analyze(args.lock)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        rows = result["packages"]
        direct_n = sum(1 for r in rows if r["relation"] == "direct")
        print(f"根项目 {result['root']['name']} {result['root']['version']}："
              f"运行时闭包 {len(rows)} 个包（直接 {direct_n}）")
        print(f"组专属包 {len(result['excluded_group_only'])} 个已排除；"
              f"组/运行时重叠（必须打包）：{result['group_overlap_must_package']}\n")
        print(f"{'包':22s}{'版本':11s}{'关系':11s}{'类型':6s}Android 打包动作")
        print("-" * 86)
        for row in rows:
            mark = "★" if row["android_action"] == "local-recipe" else " "
            kind = "纯Py" if row["pure_python"] else "原生"
            print(f"{mark} {row['name']:20s}{str(row['version']):11s}"
                  f"{row['relation']:11s}{kind:6s}{row['android_action']}")
        groups: dict[str, list[str]] = {}
        for name in result["excluded_group_only"]:
            groups.setdefault(result["excluded_group_of"][name], []).append(name)
        print("\n=== 仅由依赖组引入、不会进 APK 的包 ===")
        for group, names in sorted(groups.items()):
            print(f"  [{group}] " + ", ".join(sorted(names)))

    unknown = [r["name"] for r in result["packages"] if r["android_action"] == "unknown"]
    if unknown:
        message = f"[deps] 以下依赖尚无 Android 打包归宿（需要补 p4a recipe）：{unknown}"
        if args.fail_on_unknown:
            print(message, file=sys.stderr)
            return 1
        print("\n" + message, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
