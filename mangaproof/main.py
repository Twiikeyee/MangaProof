"""MangaProof 程序入口。

启动顺序：日志 → QApplication → 暗色主题 → 设置 → 主窗口。
程序目录判定统一走 config.paths.get_app_dir()（需求 §56、§57）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from mangaproof import APP_NAME, __version__
from mangaproof.utils.shutdown import exit_app


def apply_app_icon(app, icon_path: Path | None = None) -> Path | None:
    """加载应用图标（ico/ico.png）。

    - PySide6 直接加载 PNG，主窗口与所有对话框统一生效；
    - 图标查找顺序：显式路径 → 程序目录/ico/ico.png →
      PyInstaller 冻结资源目录（sys._MEIPASS，PyInstaller 6.x 将
      数据文件放在 onedir 的 _internal/ 下）；
    - 图标缺失时不阻塞启动（仅记录日志）。
    """
    from PySide6.QtGui import QIcon

    from mangaproof.config import paths
    from mangaproof.utils.logging_setup import get_logger

    log = get_logger("main")
    candidates: list[Path] = []
    if icon_path is not None:
        candidates.append(Path(icon_path))
    else:
        candidates.append(paths.get_app_dir() / "ico" / "ico.png")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "ico" / "ico.png")

    for path in candidates:
        if path.exists():
            app.setWindowIcon(QIcon(str(path)))
            log.info("已加载应用图标：%s", path)
            return path
    log.warning("未找到应用图标（查找：%s），继续启动", [str(p) for p in candidates])
    return None


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # 程序目录（兼容 python main.py / PyInstaller onedir）
    from mangaproof.config import paths

    app_dir = paths.get_app_dir()

    from mangaproof.utils.logging_setup import get_logger, setup_logging

    setup_logging(app_dir)
    log = get_logger("main")
    log.info("%s v%s 启动，程序目录：%s", APP_NAME, __version__, app_dir)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("MangaProof")

    from mangaproof.config.settings import SettingsManager
    from mangaproof.console import apply_console_visibility
    from mangaproof.fonts import load_app_fonts
    from mangaproof.ui.dark_titlebar import install_dark_titlebar
    from mangaproof.ui.main_window import MainWindow
    from mangaproof.ui.theme import apply_dark_theme

    # 统一字体：先注册 MiSans（直接运行 → 程序目录/font/；
    # 打包产物 → 冻结资源目录），再以其为首选字体应用主题
    font_family = load_app_fonts(app)
    apply_dark_theme(app, primary_family=font_family)
    install_dark_titlebar(app)
    apply_app_icon(app)
    settings_manager = SettingsManager()
    # 控制台可见性：直接运行 py 始终保留；打包产物默认隐藏（设置可关）
    apply_console_visibility(settings_manager.settings)

    window = MainWindow(settings_manager)
    window.show()
    # 首次使用（既没有 settings.json 也没有 recent.json）：把设置页面直接打开，
    # 让用户自己过一遍——只展示，不预设、不推荐、不写文件。用 singleShot 让
    # 主窗口先画出来，用户能看到设置是在哪个程序里弹的。
    QTimer.singleShot(0, window.maybe_prompt_first_run_settings)
    return app.exec()


if __name__ == "__main__":
    # 与仓库根 main.py 保持一致：Android 上跳过收尾（QTBUG-85449 家族），
    # 桌面仍是 sys.exit 语义。
    exit_app(main())
