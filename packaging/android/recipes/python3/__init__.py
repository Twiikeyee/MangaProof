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
# libpython3.14.so，链接器找不到 libpython3.11.so，应用启动即闪退。
#
# 另外核对过：Qt 下载站上 **所有** Android wheel（6.10.x ~ 6.11.2）都是
# cp311-cp311，没有 cp312/cp313/cp314 版本，所以设备端只能是 CPython 3.11.x。
#
# 版本选择：**3.11.5** —— p4a 最后一个正式版 2024.1.21 的 python3/hostpython3
# 就是 3.11.5，是它真正测过的组合。本项目已本地实测：develop 分支为该版本准备的
# 补丁全部干净应用（pyconfig_detection / reproducible-buildinfo /
# cpython-311-ctypes-find-library / py3.8.1_fix_cortex_a8 均 dry-run 通过，
# 仅有无害 offset/fuzz），因此"p4a develop + 版本钉 3.11.5"是安全的。
#
# 注意：
#   · 本地 recipe 会被优先搜索（p4a `Recipe.recipe_dirs()` 把 --local-recipes 排首位）
#     → 同名覆盖内置实现；
#   · `Recipe.name` 取自**目录名**（`__class__.__module__` 末段），所以类名不影响解析；
#   · hostpython3 有自己的硬编码版本，必须一并覆盖（见 ../hostpython3/）；
#   · 应用侧已 grep 确认没有 Python 3.12+ 专属写法，跑在 3.11 上没有问题
#     （pyproject 里的 requires-python >=3.12 只约束桌面环境）。

from pythonforandroid.recipes.python3 import Python3Recipe as _UpstreamPython3Recipe


class Python311Recipe(_UpstreamPython3Recipe):
    version = "3.11.5"


recipe = Python311Recipe()
