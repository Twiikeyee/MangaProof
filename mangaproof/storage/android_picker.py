"""Android 原生选择器（SAF）：通过**共享文件协议**驱动 Java 侧并取回结果。

为什么不用 `QFileDialog`
------------------------
Qt 6.11.2 自带的原生文件对话框在 Android 上有致命缺陷：系统选择器返回后，
Java 在主线程同步回调 `QtAndroidPrivate::handleActivityResult()`，该函数
**持有非递归 `QMutex`** 逐个调用监听者；监听者发出的 `accept()` / `reject()`
经 Qt 直连信号走到 `QDialog::done()` → `hide()` →
`QAndroidPlatformFileDialogHelper::hide()` → `unregisterActivityResultListener()`
→ **再次获取同一把 `QMutex`** → 主线程自锁死。表现就是：**选文件、选文件夹、
取消，三种操作都会让界面永久卡死**。因此 Android 上完全不走 `QFileDialog`。

为什么是"共享文件"而不是 JNI
---------------------------
PySide6 的 Android wheel **不向 Python 暴露任何 JNI 绑定**：实测
`PySide6/QtCore.abi3.so` 与 `libpyside6.abi3.so` 里 `QJniObject` /
`QJniEnvironment` / `QtAndroidPrivate` / `getJniType` 的命中数全部为 **0**
（wheel 里出现的 `QJniObject` 符号来自 Qt 自己的 C++ 库
`libQt6Core_arm64-v8a.so`），也没有 CPython 的 `java` / `_jni` 模块。
也就是说 **Python 无法调用任何 Java 方法**，只能用"进程内共享文件"通信。

协议（与 `packaging/android/java/.../PickerActivity.java` 必须逐字一致）
-----------------------------------------------------------------------
目录：应用私有 `files/picker/`（无需任何权限；路径优先取自 Java 写入的环境变量
`MANGAPROOF_PICKER_DIR`，取不到时按 QStandardPaths 候选路径兜底）。

    Python → Java   写 cmd.tmp 后 rename 成 cmd.txt，内容一行：FOLDER / FILE
    Java  → Python  写 result.tmp 后 rename 成 result.txt，内容一行：
                      OK<TAB><真实路径><TAB><uri>   /   CANCEL   /   ERROR<TAB><原因>

两边都用"临时文件 + rename"做**原子**写，绝不会读到半截内容。

安全性（这是本方案的关键）
------------------------
本函数**不会**像 Qt 那样进入嵌套事件循环等待结果：它只在当前线程跑一个普通
`QEventLoop`，结果到达（QTimer 每 250ms 轮询一次结果文件）或**超时**即返回。
因此即使 Java 侧完全没响应、Activity 被系统重建、选择器被强杀，界面也只会
恢复原状并给出提示，**不可能卡死**。
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from PySide6.QtCore import QEventLoop, QStandardPaths, QTimer

log = logging.getLogger("mangaproof.storage.android_picker")

#: Java 侧写进进程环境的选择器目录（由 A11yEnvProvider 在应用启动最早阶段写入）
_DIR_ENV = "MANGAPROOF_PICKER_DIR"

#: 协议文件名
_CMD_FILE = "cmd.txt"
_CMD_TMP = "cmd.tmp"
_RESULT_FILE = "result.txt"
_RESULT_TMP = "result.tmp"

#: 轮询间隔与超时：超时后**必须**恢复 UI，绝不永久卡死
_POLL_INTERVAL_MS = 250
PICK_TIMEOUT_MS = 5 * 60 * 1000

#: 结果种类
RESULT_OK = "OK"
RESULT_CANCEL = "CANCEL"
RESULT_ERROR = "ERROR"


class AndroidPickerError(RuntimeError):
    """Android 原生选择器不可用、超时或返回失败。"""


def _candidate_dirs() -> list[Path]:
    """选择器目录候选项（按可靠性排序）。

    首选 Java 侧显式告知的路径：`context.getFilesDir()/picker` —— 这是 APK 里
    Java 与 Python 一定一致的那个目录（`getFilesDir()` 就是应用私有
    `/data/data/<pkg>/files`）。QStandardPaths 只作为兜底，避免 Qt 版本差异导致
    两边指向不同目录。
    """
    dirs: list[Path] = []
    env_dir = os.environ.get(_DIR_ENV, "").strip()
    if env_dir:
        dirs.append(Path(env_dir))
    for location in (
        QStandardPaths.StandardLocation.AppDataLocation,
        QStandardPaths.StandardLocation.AppLocalDataLocation,
        QStandardPaths.StandardLocation.GenericDataLocation,
    ):
        raw = QStandardPaths.writableLocation(location)
        if raw:
            dirs.append(Path(raw) / "picker")
            dirs.append(Path(raw))
    # 去重且保持顺序
    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def picker_dir() -> Path:
    """返回可用的选择器目录（不存在时尝试创建）。

    若 Java 侧已发布 `MANGAPROOF_PICKER_DIR`，**只认它**：这是两侧一定会一致的
    目录（`getFilesDir()/picker`）。否则才按 QStandardPaths 候选兜底。

    这条优先级很重要：X11/桌面环境下 `QStandardPaths.AppDataLocation` 会返回
    如 `~/.local/share` 这样的**已存在**目录，若允许它参与"已存在优先"，就会在
    Android 上悄悄选错目录，导致 Java 与 Python 各写各的文件、表现为"选择器永远
    没反应"。
    """
    env_dir = os.environ.get(_DIR_ENV, "").strip()
    if env_dir:
        path = Path(env_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AndroidPickerError(f"无法创建选择器目录 {path}：{exc}") from exc
        return path

    candidates = _candidate_dirs()
    for path in candidates:
        if path.is_dir():
            return path
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            log.info("创建选择器目录：%s", path)
            return path
        except OSError:
            continue
    raise AndroidPickerError(
        "找不到（也无法创建）选择器目录，候选：" + "、".join(str(p) for p in candidates)
    )


def _write_atomic(target: Path, text: str) -> None:
    """先写临时文件再 rename，保证 Java 侧永远读不到半截内容。"""
    tmp = target.with_name(target.name[:-len(".txt")] + ".tmp" if target.name.endswith(".txt") else target.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, target)          # 同目录内 rename 是原子操作


def _read_result(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:           # pragma: no cover - 极少见
        log.warning("读取选择结果失败：%s", exc)
        return None


def _parse(line: str) -> dict:
    parts = line.split("\t")
    kind = parts[0].strip().upper() if parts else ""
    if kind == RESULT_OK:
        path = parts[1] if len(parts) > 1 else ""
        uri = parts[2] if len(parts) > 2 else ""
        if not path:
            return {"status": RESULT_ERROR, "path": "", "uri": "", "message": "系统返回了空路径"}
        return {"status": RESULT_OK, "path": path, "uri": uri, "message": ""}
    if kind == RESULT_CANCEL:
        return {"status": RESULT_CANCEL, "path": "", "uri": "", "message": ""}
    if kind == RESULT_ERROR:
        return {
            "status": RESULT_ERROR,
            "path": "",
            "uri": "",
            "message": parts[1] if len(parts) > 1 else "未知原因",
        }
    return {"status": RESULT_ERROR, "path": "", "uri": "", "message": f"无法识别的结果：{line[:120]!r}"}


def pick(kind: str, *, timeout_ms: int = PICK_TIMEOUT_MS) -> dict:
    """拉起系统选择器并等待结果（**不阻塞、会超时退出**）。

    :param kind: ``"folder"`` 或 ``"file"``
    :param timeout_ms: 最长等待时间；到点即返回 ERROR，绝不永久卡住界面
    :return: ``{"status": OK|CANCEL|ERROR, "path": str, "uri": str, "message": str}``
    """
    if kind not in ("folder", "file"):
        raise ValueError(f"未知的选择类型：{kind!r}")

    directory = picker_dir()
    cmd_path = directory / _CMD_FILE
    result_path = directory / _RESULT_FILE

    # 上一轮的残留结果先清掉，避免把旧结果当成本次结果
    for stale in (result_path, directory / _RESULT_TMP):
        try:
            stale.unlink()
        except OSError:
            pass
    # 还有未消费的命令 = 上一次选择没结束（用户可能还停在系统界面里）
    if cmd_path.is_file():
        return {
            "status": RESULT_ERROR,
            "path": "",
            "uri": "",
            "message": "上一次选择尚未结束，请先完成或返回应用",
        }

    token = uuid.uuid4().hex[:8]
    command = "FOLDER" if kind == "folder" else "FILE"
    _write_atomic(cmd_path, f"{command} {token}")
    log.info("已请求系统选择器：%s（token=%s，目录=%s）", command, token, directory)

    state = {"settled": False, "timeout": False}
    result: dict = {
        "status": RESULT_ERROR,
        "path": "",
        "uri": "",
        "message": "等待系统选择器超时（没有收到结果）。请重试。",
    }

    loop = QEventLoop()

    def _poll() -> None:
        """读结果文件；读到就解析并结束等待。"""
        line = _read_result(result_path)
        if line is None:
            return
        result.update(_parse(line))
        state["settled"] = True
        try:
            result_path.unlink()
        except OSError:
            pass
        loop.quit()

    def _on_timeout() -> None:
        state["timeout"] = True
        loop.quit()

    poller = QTimer()
    poller.setInterval(_POLL_INTERVAL_MS)
    poller.timeout.connect(_poll)
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(_on_timeout)

    poller.start()
    guard.start(timeout_ms)
    _poll()                       # 结果可能已经写好（极快返回的情形）
    if not state["settled"]:
        loop.exec()               # 普通事件循环：不是模态对话框，也没有嵌套等待
    poller.stop()
    guard.stop()
    poller.deleteLater()
    guard.deleteLater()

    if state["timeout"]:
        # 超时：清掉命令，避免 Java 侧稍后又弹出一次选择器
        try:
            cmd_path.unlink()
        except OSError:
            pass
        log.warning("等待系统选择器超时（%d ms）", timeout_ms)
    return result


def probe() -> dict:
    """自检：目录与协议文件是否可用（只读，不弹 UI；供日志/诊断使用）。"""
    info: dict[str, object] = {"dir_env": os.environ.get(_DIR_ENV, "")}
    try:
        directory = picker_dir()
        info["dir"] = str(directory)
        info["dir_ok"] = True
        info["cmd_pending"] = (directory / _CMD_FILE).is_file()
    except Exception as exc:
        info["dir_ok"] = False
        info["dir_error"] = str(exc)
    return info
