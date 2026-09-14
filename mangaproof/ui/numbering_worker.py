"""后台问题编号检查/重排线程（进度框 + 防 GUI 卡死）。

编号扫描是只读操作（见 review/numbering.plan_numbering），放到 QThread 执行，
通过 progress(done, total, message) 把阶段信息推给 UI 的进度对话框
（与「打开任务」「生成返修单」同一套风格）。

方案计算结果交回主线程后一次性写回任务并保存：worker 不修改任务，
避免与界面读取（红框绘制、问题面板）并发写同一份数据。

取消：主线程调用 request_cancel()，在下一次进度回调处抛出 NumberingCancelled
中断——此时尚未写回任务，任务数据保持不变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Signal

from mangaproof.review.numbering import NumberingPlan, plan_numbering
from mangaproof.review.state import TaskState

log = logging.getLogger("mangaproof.ui.numbering_worker")

KIND_OK = "ok"                 # 检查完成
KIND_CANCELLED = "cancelled"   # 用户取消


class NumberingCancelled(Exception):
    """用户取消编号检查（由 progress_cb 抛出，逐级向上传播）。"""


@dataclass
class NumberingResult:
    kind: str
    plan: Optional[NumberingPlan] = None


class NumberingWorker(QThread):
    """后台检查问题编号并产出重排方案。

    progress(done, total, message)：文件级进度；
    succeeded(NumberingResult)：完成（含用户取消）；
    failed(str)：致命错误。
    """

    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        task: TaskState,
        layer_ids_by_file: Dict[str, List[str]],
        parent=None,
    ):
        super().__init__(parent)
        self._task = task
        self._layer_ids = layer_ids_by_file
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    # -- 线程入口 ----------------------------------------------------------

    def run(self) -> None:
        try:
            plan = plan_numbering(self._task, self._layer_ids, progress_cb=self._cb)
        except NumberingCancelled:
            self.succeeded.emit(NumberingResult(kind=KIND_CANCELLED))
            return
        except Exception as exc:
            log.exception("检查问题编号失败")
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(NumberingResult(kind=KIND_OK, plan=plan))

    # -- 内部 --------------------------------------------------------------

    def _cb(self, done: int, total: int, message: str) -> None:
        """进度回调：转发进度；用户取消时抛出 NumberingCancelled 中断。"""
        self.progress.emit(done, total, message)
        if self._cancel:
            raise NumberingCancelled()
