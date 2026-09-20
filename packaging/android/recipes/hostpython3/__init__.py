# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

# MangaProof Android recipe —— hostpython3（构建期解释器，必须与 python3 同版本）
#
# p4a 的 hostpython3 recipe 有**自己的硬编码版本**（develop 分支同为 3.14.2），
# 不会从 python3 recipe 同步；而它自己的 download() 里有一道强制校验：
#
#     if python_recipe.version != self.version:
#         raise BuildInterruptingException(
#             f"python3 should have same version as hostpython3, ...")
#
# 所以必须与 ../python3/ 一起钉到 3.11.5。
#
# 背景见 ../python3/__init__.py：Qt 官方 Android wheel 是 cp311 构建，
# 原生模块硬依赖 libpython3.11.so。
#
# 同样覆盖 `get_recipe_dir()`：本 recipe 自带 `fix_ensurepip.patch`，
# 而 p4a 的 apply_patch() 用 get_recipe_dir() 定位补丁 —— 若不指回上游目录，
# 会在 "Applying patch fix_ensurepip.patch" 处报文件不存在
# （CI run 34919720480 即如此）。

from pathlib import Path

import pythonforandroid.recipes.hostpython3 as _upstream_hostpython3_module
from pythonforandroid.recipes.hostpython3 import HostPython3Recipe as _UpstreamHostPython3Recipe


class HostPython311Recipe(_UpstreamHostPython3Recipe):
    version = "3.11.5"

    def get_recipe_dir(self):
        """resource（fix_ensurepip.patch）仍从 p4a 源码树读取。"""
        return str(Path(_upstream_hostpython3_module.__file__).resolve().parent)


recipe = HostPython311Recipe()
