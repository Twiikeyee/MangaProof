# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""问题编号检查与重排（主界面标注修正）。

问题编号（``Issue.issue_no``）在新增问题时取「当前最大值 + 1」：删除问题、
或监制完后面的 PSD 又回到前面补问题时，编号就会出现空号、跳号，
界面上（红框徽标、问题面板 ``#编号``）看起来就是「号码不对」。

本模块提供按 **文档顺序**（PSD 顺序 → 图层顺序 → 创建顺序）的检查与重排：

- :func:`plan_numbering`：只读扫描，产出「问题 → 新编号」方案，
  不修改任务，因此可以放到后台线程执行（大任务不冻结界面）；
- :func:`apply_numbering`：把方案写回任务（主线程一次性完成，开销可忽略）。

重排只在用户显式触发时执行（见主窗口「检查问题编号」按钮）：新增/删除问题
时不做编号维护，避免每次标记都全量重排。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from mangaproof.review.issue import Issue
from mangaproof.review.state import TaskState

log = logging.getLogger("mangaproof.review.numbering")

# 进度回调：(已完成步数, 总步数, 阶段说明)
ProgressCb = Callable[[int, int, str], None]

# 图层顺序未知（文件未扫描/图层已不存在）时的排序哨兵：排在已知图层之后
_RANK_MISSING = 1 << 30


@dataclass
class NumberingPlan:
    """编号方案（不修改任务本身）。"""

    entries: List[Tuple[Issue, int]] = field(default_factory=list)  # (问题, 新编号)
    total: int = 0          # 问题总数
    fixed: int = 0          # 编号需要变更的问题数
    orphans: int = 0        # 所属 PSD/图层已不存在的问题数（排在最后）
    files: int = 0          # 参与排序的 PSD 数

    @property
    def changed(self) -> bool:
        return self.fixed > 0 or self.orphans > 0


def _emit(
    progress_cb: Optional[ProgressCb], done: int, total: int, message: str
) -> None:
    if progress_cb is not None:
        progress_cb(done, total, message)


def plan_numbering(
    task: TaskState,
    layer_ids_by_file: Dict[str, List[str]],
    progress_cb: Optional[ProgressCb] = None,
) -> NumberingPlan:
    """扫描任务并产出编号方案（只读，可放后台线程）。

    排序键：PSD 在任务中的顺序 → 图层在文档中的顺序 → 同图层内按原编号
    （即创建顺序，稳定）。所属 PSD 已从任务移除、或所属图层已不存在的
    问题（orphan）不丢弃，统一排在最后并计入 ``orphans``，由界面提示用户。

    只有「确实有问题」的 PSD 才建立图层顺序索引（大任务下避免为整本书
    建表），因此开销与问题数量成正比而非与书页数成正比。
    """
    files = list(task.files)
    file_rank = {rec.relative_path: idx for idx, rec in enumerate(files)}

    grouped: Dict[str, List[Issue]] = {}
    for issue in task.issues:
        grouped.setdefault(issue.file, []).append(issue)

    layer_rank: Dict[str, Dict[str, int]] = {
        rel: {lid: idx for idx, lid in enumerate(layer_ids_by_file[rel])}
        for rel in grouped
        if rel in layer_ids_by_file
    }

    total_steps = len(files) + 1
    ordered: List[Issue] = []
    orphans: List[Issue] = []

    for i, record in enumerate(files):
        rel = record.relative_path
        _emit(
            progress_cb, i, total_steps,
            f"检查问题编号（{i + 1}/{len(files)}）：{record.file_name}",
        )
        items = grouped.get(rel, [])
        if not items:
            continue
        ranks = layer_rank.get(rel)
        known: List[Issue] = []
        for issue in items:
            if ranks is None:
                # 该 PSD 未扫描图层（解析失败等）：保留问题，排在文件末尾
                known.append(issue)
            elif issue.layer_id in ranks:
                known.append(issue)
            else:
                # 图层已不存在（PSD 被改动）→ 归属不明，排到最后
                orphans.append(issue)
        known.sort(
            key=lambda x: (
                ranks.get(x.layer_id, _RANK_MISSING) if ranks else _RANK_MISSING,
                x.issue_no,
            )
        )
        ordered.extend(known)

    # 任务文件列表之外的问题（PSD 已从任务移除）→ 同样排在最后
    extra = [i for i in task.issues if i.file not in file_rank]
    extra.sort(key=lambda x: x.issue_no)
    orphans.extend(extra)

    _emit(progress_cb, len(files), total_steps, "重排问题编号…")
    all_items = ordered + orphans
    entries: List[Tuple[Issue, int]] = []
    fixed = 0
    for number, issue in enumerate(all_items, start=1):
        if issue.issue_no != number:
            fixed += 1
        entries.append((issue, number))
    _emit(progress_cb, total_steps, total_steps, "编号检查完成")

    return NumberingPlan(
        entries=entries,
        total=len(entries),
        fixed=fixed,
        orphans=len(orphans),
        files=len(files),
    )


def apply_numbering(task: TaskState, plan: NumberingPlan) -> int:
    """把编号方案写回任务（主线程调用，返回修正的编号数量）。

    同时把 ``task.issues`` 调整为方案顺序，保证「列表顺序 = 编号顺序」，
    便于阅读与持久化。
    """
    for issue, number in plan.entries:
        issue.issue_no = number
    task.issues = [issue for issue, _ in plan.entries]
    log.info(
        "问题编号重排完成：共 %d 个，修正 %d 个，归属不明 %d 个",
        plan.total, plan.fixed, plan.orphans,
    )
    return plan.fixed
