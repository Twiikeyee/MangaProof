"""返修单文案模板（需求 §45、§50、§54）。"""

REPORT_TITLE = "MangaProof 返修单"
REPORT_TITLE_EN = "MangaProof Revision Sheet"

LABEL_TASK = "任务"
LABEL_GENERATED_AT = "生成时间"
LABEL_PSD_COUNT = "PSD"
LABEL_LAYER_COUNT = "图层"
LABEL_PASSED = "通过"
LABEL_FAILED = "未通过"
LABEL_UNREVIEWED = "未监制"
LABEL_REVIEWED = "已监制"
LABEL_ISSUES = "问题"
LABEL_STATUS = "任务状态"

STATUS_COMPLETE = "已完成"
STATUS_INCOMPLETE = "未完成"

TOC_TITLE = "目录"
TOC_HINT = "点击目录条目或使用阅读器书签（大纲）可直接跳转到对应 PSD 页。"

OVERVIEW_TITLE = "PSD 总览"
DETAIL_TITLE = "问题明细"
# 隐藏干净页（全部通过且无问题）时的说明 / 全部干净时的提示
OVERVIEW_HIDDEN_FMT = (
    "注：已隐藏 {0} 个无问题的 PSD（全部图层通过）；"
    "未通过、未监制的 PSD 均如实列出。"
)
OVERVIEW_ALL_CLEAN = "本任务全部 PSD 均已通过且没有问题。"

COLUMN_FILE = "文件"
COLUMN_PROGRESS = "进度"
COLUMN_ISSUE_COUNT = "问题数"

LAYER_LABEL = "图层"
TYPE_LABEL = "问题"
COMMENT_LABEL = "批注"
