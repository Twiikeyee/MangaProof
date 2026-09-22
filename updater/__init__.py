# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MangaProof 自更新安装器（需求 §40~§64，调研报告 §5/§11.2~§11.4）。

这是一个**完全独立**于主程序运行环境的程序包（需求 §43）：只用标准库 +
``mangaproof`` 里三个**零依赖**模块（``config.user_data`` / ``utils.hashing`` /
``update.checksum``），不引入 PySide6 / httpx / keyring。

模块分工（需求 §79 的安装器结构）：

- :mod:`updater.archive`   —— 更新包格式、结构校验、安全解压（§50~§52）
- :mod:`updater.backup`    —— 用户数据白名单复制/恢复（§47~§49、§56）
- :mod:`updater.verify`    —— 破坏性操作前的最终校验（§53）
- :mod:`updater.state`     —— 阶段状态与成功标记的原子落盘（§58/§59/§64）
- :mod:`updater.rollback`  —— 回滚与异常中断恢复（§63/§64）
- :mod:`updater.privilege` —— 平台专有提权（§39/§42），平台逻辑运行时导入
- :mod:`updater.installer` —— 安装器状态机（§78）
- :mod:`updater.ui`        —— Tkinter GUI / 控制台降级（§43/§11.2/§11.4）
- :mod:`updater.main`      —— CLI 入口与日志（§45）

可执行文件名为 ``MangaProof-update-installer``（Windows 加 ``.exe``，需求 §43），
由 ``packaging/installer.spec``（PyInstaller onefile）构建，随主程序发布包
**与主程序可执行文件同级**分发（调研报告 §11.3）。

安装器本体**不提权启动**：需要管理员权限时只把安装逻辑（``--cli`` 子进程）交给
平台原生授权机制执行，提权只用于文件操作（调研报告 §5.1~§5.3）。
"""

from __future__ import annotations

#: 安装器可执行文件名（不含 Windows 的 .exe 后缀）
INSTALLER_NAME = "MangaProof-update-installer"

#: 旧版本目录后缀（需求 §54/§55：先改名再解压）
OLD_SUFFIX = ".old"

__all__ = ["INSTALLER_NAME", "OLD_SUFFIX"]
