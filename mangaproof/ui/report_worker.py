# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""后台返修单生成线程（进度框 + 防 GUI 卡死）。

PDF 生成的耗时集中在「逐页提取 merged image → 编码 PNG → 写入 PDF」，
全部放到 QThread 执行，通过 progress(done, total, message) 把阶段信息推给
UI 的进度对话框（与「打开任务」进度框同一套风格），大批量任务不再冻结界面。

取消：主线程调用 request_cancel()，在下一次进度回调处抛出 ReportCancelled
中断生成——此时 PDF 尚未开始落盘，已存在的同名返修单不受影响。

文档对象：优先复用主线程传入的已打开文档快照，缺失的按需在本线程重建；
worker 自持文档字典，不改动主窗口的 _docs 缓存（图层像素仍走线程安全的
共享 LRU，关闭 worker 后本线程新建的文档随对象释放）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Signal

from mangaproof.psd.document import PSDDocument
from mangaproof.report.generator import ReportCancelled, generate_report
from mangaproof.review.state import TaskState

log = logging.getLogger("mangaproof.ui.report_worker")

KIND_OK = "ok"                 # 生成完成
KIND_CANCELLED = "cancelled"   # 用户取消


@dataclass
class ReportResult:
    kind: str
    path: Optional[Path] = None


class ReportWorker(QThread):
    """后台生成返修单。

    progress(done, total, message)：页面级进度；
    succeeded(ReportResult)：完成（含用户取消）；
    failed(str)：致命错误。
    """

    progress = Signal(int, int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        task: TaskState,
        layer_ids_by_file: Dict[str, List[str]],
        report_path: Path,
        base_dir: Path,
        docs: Optional[Dict[str, object]] = None,   # rel -> 已打开文档快照
        layer_cache=None,                           # 共享图层像素 LRU
        image_format: str = "png",                  # "png" 无损 / "jpeg" 压缩
        image_quality: int = 80,                    # JPEG 质量
        hide_clean_files: bool = True,              # 总览表隐藏无问题的 PSD
        parent=None,
    ):
        super().__init__(parent)
        self._task = task
        self._layer_ids = layer_ids_by_file
        self._out_path = Path(report_path)
        self._base_dir = Path(base_dir)
        self._docs: Dict[str, object] = dict(docs) if docs else {}
        self._layer_cache = layer_cache
        self._image_format = image_format
        self._image_quality = image_quality
        self._hide_clean_files = hide_clean_files
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    # -- 线程入口 ----------------------------------------------------------

    def run(self) -> None:
        try:
            generate_report(
                self._task,
                self._layer_ids,
                self._out_path,
                self._image_provider,
                progress_cb=self._cb,
                image_format=self._image_format,
                image_quality=self._image_quality,
                hide_clean_files=self._hide_clean_files,
            )
        except ReportCancelled:
            log.info("返修单生成已取消：%s", self._out_path)
            self.succeeded.emit(ReportResult(kind=KIND_CANCELLED))
            return
        except Exception as exc:
            log.exception("生成返修单失败：%s", self._out_path)
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(ReportResult(kind=KIND_OK, path=self._out_path))

    # -- 内部 --------------------------------------------------------------

    def _cb(self, done: int, total: int, message: str) -> None:
        """进度回调：转发进度；用户取消时抛出 ReportCancelled 中断。"""
        self.progress.emit(done, total, message)
        if self._cancel:
            raise ReportCancelled()

    def _image_provider(self, rel: str):
        """按需提供文档对象（仅在本线程内创建，不动主窗口缓存）。"""
        doc = self._docs.get(rel)
        if doc is None:
            try:
                doc = PSDDocument(self._base_dir / rel, layer_cache=self._layer_cache)
            except Exception:
                log.exception("无法读取该 PSD 文件：%s", rel)
                return None
            self._docs[rel] = doc
        return doc
