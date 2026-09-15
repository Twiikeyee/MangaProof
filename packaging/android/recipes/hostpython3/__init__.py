# MangaProof Android recipe —— hostpython3（构建期解释器，必须与 python3 同版本）
#
# p4a 的 hostpython3 recipe 有**自己的硬编码版本**（develop 分支同为 3.14.2），
# 并不从 python3 recipe 同步；只改 python3 会造成 host/target 版本不一致。
# 因此这里一并钉到 3.11.5，与 ../python3/ 保持一致。
#
# 背景见 ../python3/__init__.py：Qt 官方 Android wheel 是 cp311 构建，
# 原生模块硬依赖 libpython3.11.so。

from pythonforandroid.recipes.hostpython3 import HostPython3Recipe as _UpstreamHostPython3Recipe


class HostPython311Recipe(_UpstreamHostPython3Recipe):
    version = "3.11.5"


recipe = HostPython311Recipe()
