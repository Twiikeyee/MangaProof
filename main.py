# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MangaProof 启动入口（需求 §56：python main.py）。

程序目录 = 本文件所在目录（config.paths.get_app_dir() 会自动判定）。
"""

from mangaproof.main import main
from mangaproof.utils.shutdown import exit_app

if __name__ == "__main__":
    # 退出按平台分流：Android 上跳过 CPython 收尾与 Qt/C++ 析构，直接 os._exit()
    # （Qt for Android 退出阶段会崩，即 QTBUG-85449 家族）；桌面保持 sys.exit 语义。
    # 数据与日志都不依赖收尾流程（closeEvent 已落盘、日志逐条 flush），详见
    # mangaproof/utils/shutdown.py 的说明。
    exit_app(main())
