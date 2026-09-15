# MangaProof Android recipe —— python3（设备端解释器，钉到 3.11.x）
#
# 为什么必须覆盖：Qt 官方 Android wheel 是 **cp311** 构建的，而 wheel 里的原生模块
# 硬编码依赖 `libpython3.11.so`（readelf -d 实测）：
#
#     QtCore.abi3.so       NEEDED libpython3.11.so
#     libshiboken6.abi3.so NEEDED libpython3.11.so
#     libpyside6.abi3.so   NEEDED libpython3.11.so
#     Shiboken.abi3.so     NEEDED libpython3.11.so
#
# 而 p4a develop 分支的 python3 recipe 是 `3.14.2` → 编出来的 APK 装的是
# libpython3.14.so，链接器找不到 libpython3.11.so，应用启动即闪退（已实测复现）。
#
# 另外核对过：Qt 下载站上 **所有** Android wheel（6.10.x ~ 6.11.2）都是
# cp311-cp311，没有 cp312/cp313/cp314 版本，所以设备端只能是 CPython 3.11.x。
#
# 版本选择：**3.11.5** —— p4a 最后一个正式版 2024.1.21 的 python3/hostpython3
# 就是 3.11.5，是它真正测过的组合。本项目已本地实测：develop 分支为该版本准备的
# 4 个补丁全部干净应用（patch --dry-run 通过，仅无害 offset/fuzz）。
#
# ⚠️ 关键细节：必须把 recipe 目录指回 p4a 源码树
#   p4a 的 `Recipe.get_recipe_dir()` 会**优先**返回 `--local-recipes` 下的同名目录，
#   而 `apply_patch()` 用 `join(self.get_recipe_dir(), filename)` 定位补丁
#   （recipe.py:289）→ 一旦返回我们的目录，上游的 `patches/*.patch` 就找不到，
#   构建会在 "Applying patches" 阶段挂掉（CI run 34919720480 即如此）。
#   因此这里覆盖 `get_recipe_dir()`：只改版本号，recipe 自带文件仍走上游。
#
# 其它：`Recipe.name` 取自目录名（`__class__.__module__` 末段），类名不影响解析；
#      hostpython3 有自己的硬编码版本且**强制要求与 python3 一致**
#      （其 download() 里有版本校验），见 ../hostpython3/。

from pathlib import Path

import pythonforandroid.recipes.python3 as _upstream_python3_module
from pythonforandroid.recipes.python3 import Python3Recipe as _UpstreamPython3Recipe


class Python311Recipe(_UpstreamPython3Recipe):
    version = "3.11.5"

    def get_recipe_dir(self):
        """resource（补丁）仍从 p4a 源码树读取，避免本地覆盖目录里缺文件。"""
        return str(Path(_upstream_python3_module.__file__).resolve().parent)


recipe = Python311Recipe()
