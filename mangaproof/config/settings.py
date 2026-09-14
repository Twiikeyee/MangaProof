"""程序级设置：settings.json（需求 §20.2、§30、§35、§55）。

软件级设置全部落在 程序目录/settings.json，与任务数据（.mangaproof.json）
彻底分离（需求 §58）。

注意：**最近打开记录不在这里**——它存在同目录的独立文件 recent.json
（见 config/recent.py）。settings.json 一旦载入旧版残留的 recent_paths，
只做一次性迁移读取，之后不再写回。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mangaproof.config import paths

log = logging.getLogger("mangaproof.config.settings")

SETTINGS_VERSION = 1

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

DEFAULT_KEYBINDINGS: dict[str, str] = {
    "prev_psd": "Up",
    "next_psd": "Down",
    "prev_layer": "Left",
    "next_layer": "Right",
    "pass_layer": "Return",
    "fail_layer": "/",
    "toggle_compare": "Space",
    "cancel_operation": "Esc",
    "save_task": "Ctrl+S",
    "custom_comment": "Ctrl+Return",
    "open_psd": "Ctrl+O",
    "open_folder": "Ctrl+Shift+O",
    "close_task": "Ctrl+W",
    "generate_report": "Ctrl+R",
    "redraw_mode": "R",
    "auto_box": "A",
}

# 核心快捷键的中文名（设置对话框表格 + 冲突提示共用）
CORE_SHORTCUT_LABELS: dict[str, str] = {
    "prev_psd": "上一个 PSD",
    "next_psd": "下一个 PSD",
    "prev_layer": "上一个图层",
    "next_layer": "下一个图层",
    "pass_layer": "当前图层通过",
    "fail_layer": "当前图层未通过",
    "toggle_compare": "自动对比",
    "cancel_operation": "取消/退出批注操作",
    "save_task": "保存任务",
    "custom_comment": "自定义批注",
    "open_psd": "打开单个 PSD",
    "open_folder": "打开文件夹",
    "close_task": "关闭当前任务",
    "generate_report": "生成返修单",
    "redraw_mode": "红框模式",
    "auto_box": "自动框选当前图层",
}

# 预制问题类型（需求 §34）及其默认快捷键（需求 §35，均可配置）
#
# 键位分配：数字行 1~9、0 给最常用的前 10 类（文字外观类问题集中在 1~6），
# 接着走字母上排 Q W E _ T Y U I O P（R 让给核心快捷键「红框模式」，
# 否则两者同绑 R 会被 Qt 判为歧义快捷键而**两个都不触发**），
# 最后两类用主行的 S、D 补足。
DEFAULT_ISSUE_TYPES: list[dict[str, str]] = [
    {"name": "居中错误", "key": "1"},
    {"name": "字体选择错误", "key": "2"},
    {"name": "字体字重错误", "key": "3"},
    {"name": "文字描边粗细错误", "key": "4"},
    {"name": "文字颜色错误", "key": "5"},
    {"name": "字号错误", "key": "6"},
    {"name": "文字位置错误", "key": "7"},
    {"name": "文字间距错误", "key": "8"},
    {"name": "气泡处理错误", "key": "9"},
    {"name": "原文字擦除错误", "key": "0"},
    {"name": "背景擦除错误", "key": "Q"},
    {"name": "网点对齐错误", "key": "W"},
    {"name": "网点残留", "key": "E"},
    {"name": "修图瑕疵", "key": "T"},
    {"name": "漏翻", "key": "Y"},
    {"name": "漏字", "key": "U"},
    {"name": "错字", "key": "I"},
    {"name": "翻译错误", "key": "O"},
    {"name": "排版错误", "key": "P"},
    {"name": "文字溢出", "key": "S"},
    {"name": "其他", "key": "D"},
]

# 问题类型表版本：默认键位表变更时 +1，载入旧 settings.json 时据此一次性升级
# （见 SettingsManager._migrate_issue_types）。v2：新增「文字描边粗细错误」
# 「文字颜色错误」并整体重排键位 + 下拉栏显示快捷键。
ISSUE_TYPES_VERSION = 2

# v1（旧版）默认键位表：用于判断用户是否改过某个类型的键位——
# 与旧默认值相同的（没动过）跟随新表，用户自己改过的保持不动。
_LEGACY_ISSUE_KEYS: dict[str, str] = {
    "居中错误": "1",
    "字体选择错误": "2",
    "字体字重错误": "3",
    "字号错误": "4",
    "文字位置错误": "5",
    "文字间距错误": "6",
    "气泡处理错误": "7",
    "原文字擦除错误": "8",
    "背景擦除错误": "9",
    "网点对齐错误": "0",
    "网点残留": "Q",
    "修图瑕疵": "W",
    "漏翻": "E",
    "漏字": "R",   # v1 与「红框模式 R」撞车，v2 起为 U
    "错字": "T",
    "翻译错误": "Y",
    "排版错误": "U",
    "文字溢出": "I",
    "其他": "O",
}

# 旧版本默认值：漏字 = R 与「红框模式」= R 撞车（Qt 歧义 → 两个都失效）。
# 载入旧 settings.json 时按此表自动让位，见 SettingsManager._heal_shortcut_conflicts。
_LEGACY_DUPLICATE_ISSUE_KEYS: dict[str, str] = {"漏字": "P"}


def normalize_key(seq: str) -> str:
    """快捷键序列归一化（仅用于比较是否撞车，不改变实际绑定值）。"""
    return "".join(str(seq).split()).upper()


def shortcut_entries(
    keybindings: dict[str, str],
    issue_types: list[dict[str, str]],
    custom_comment_key: str = "",
) -> list[tuple[str, str, str]]:
    """全部快捷键绑定 → [(序列, 分类, 名称)]（跳过空绑定）。"""
    entries: list[tuple[str, str, str]] = []
    for action, label in CORE_SHORTCUT_LABELS.items():
        seq = keybindings.get(action, DEFAULT_KEYBINDINGS.get(action, ""))
        if action == "custom_comment" and custom_comment_key:
            seq = custom_comment_key
        if seq:
            entries.append((str(seq), "核心快捷键", label))
    for item in issue_types:
        key = str(item.get("key", "") or "")
        if key:
            entries.append((key, "问题类型", str(item.get("name", ""))))
    return entries


def shortcut_conflicts(
    keybindings: dict[str, str],
    issue_types: list[dict[str, str]],
    custom_comment_key: str = "",
) -> dict[str, list[tuple[str, str]]]:
    """找出重复绑定的快捷键 → {序列: [(分类, 名称), ...]}。

    Qt 对同一窗口内重复的快捷键会判定为「歧义」（Ambiguous shortcut
    overload）并拒绝触发其中任何一个——表现为按键完全没反应。
    因此配置阶段必须能检查出来：设置对话框禁止保存冲突配置，
    运行时也会提示具体是哪两个动作撞车。
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for seq, kind, label in shortcut_entries(keybindings, issue_types, custom_comment_key):
        groups.setdefault(normalize_key(seq), []).append((kind, label))
    return {seq: names for seq, names in groups.items() if len(names) > 1}

DISPLAY_RATIOS: list[float] = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
DEFAULT_DISPLAY_RATIO = 0.6

# 自动对比预设速度档位：(次/秒, 档位名)。默认"正常"= 4 次/秒，
# 即每张停留 250ms，与原硬编码行为一致（需求 §22）。
COMPARE_SPEED_TIERS: list[tuple[int, str]] = [
    (1, "慢"),
    (2, "较慢"),
    (4, "正常"),
    (5, "较快"),
    (8, "快"),
]
DEFAULT_COMPARE_SPEED_HZ = 4
DEFAULT_COMPARE_MODE = "auto"   # "auto" 自动切换 / "manual" 手动切换

# 裸滚轮（不按修饰键）行为：默认上下平移；可切换为缩放（需求 §27）。
# 触控板双指滚不受此设置影响，恒为双轴平移。
DEFAULT_WHEEL_MODE = "pan"      # "pan" 上下移动 / "zoom" 缩放

# 问题红框显示范围（需求 §39 Overlay 展示）：
# - "page"（默认）：始终显示当前 PSD（页）的全部问题——跨图层，一屏看全整页标注；
# - "layer"：只显示当前图层的问题（旧版本行为）。
ISSUE_SCOPES: tuple[str, ...] = ("page", "layer")
DEFAULT_ISSUE_SCOPE = "page"

# 当前图层视觉边界虚线框（蓝色）：定位辅助 Overlay，默认显示。
# 关闭后画布上不再绘制该框（定位、缩放行为完全不受影响）。
DEFAULT_SHOW_LAYER_OUTLINE = True

# 连续标注：默认关闭——进入拖框模式（红框模式 R / 问题类型快捷键）后，
# 标完一个问题即自动退出该模式；开启后问题面板右侧按钮常驻高亮，
# 可连续拖框标注（问题类型快捷键则保持同一类型待标）。
DEFAULT_CONTINUOUS_ANNOTATION = False

# 返修单页面图像格式：
# - "png"（默认）：无损，体积大；
# - "jpeg"：有损压缩，体积显著变小（漫画页面常见网点/渐变），质量可调。
REPORT_IMAGE_FORMATS: tuple[str, ...] = ("png", "jpeg")
DEFAULT_REPORT_IMAGE_FORMAT = "png"
JPEG_QUALITY_CHOICES: tuple[int, ...] = (60, 70, 80, 90, 95)
DEFAULT_JPEG_QUALITY = 80

# 内存回收策略档位：宽松 / 平衡 / 激进。
# 各档预算（bg QImage 池字节上限、图层像素 LRU 字节上限）在
# mangaproof/ui/main_window.py 的 _MEMORY_POLICIES 中定义；
# 文档结构卸载（窗口外驱逐）三档一致。
MEMORY_POLICIES: tuple[str, ...] = ("relaxed", "balanced", "aggressive")
DEFAULT_MEMORY_POLICY = "balanced"


@dataclass
class Settings:
    """运行时设置对象。"""

    layer_display_ratio: float = DEFAULT_DISPLAY_RATIO
    compare_mode: str = DEFAULT_COMPARE_MODE   # "auto" / "manual"
    compare_speed_hz: int = DEFAULT_COMPARE_SPEED_HZ
    wheel_mode: str = DEFAULT_WHEEL_MODE       # "pan" / "zoom"
    issue_scope: str = DEFAULT_ISSUE_SCOPE     # "page"（当前页全部）/ "layer"（仅当前图层）
    # 当前图层视觉边界虚线框（蓝色）是否显示（默认显示）
    show_layer_outline: bool = DEFAULT_SHOW_LAYER_OUTLINE
    # 连续标注（默认关：标完一个问题自动退出拖框模式）
    continuous_annotation: bool = DEFAULT_CONTINUOUS_ANNOTATION
    recursive_scan: bool = False
    generate_pdf_on_complete: bool = True
    report_name: str = ""
    # 返修单页面图像：png（无损）/ jpeg（压缩，体积小）+ JPEG 质量
    report_image_format: str = DEFAULT_REPORT_IMAGE_FORMAT
    report_jpeg_quality: int = DEFAULT_JPEG_QUALITY
    # 返修单 PSD 总览表是否隐藏「全部通过且无问题」的页（默认隐藏）
    report_hide_clean_files: bool = True
    hide_console: bool = True   # 打包产物隐藏控制台（直接运行 py 时始终显示）
    keybindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_KEYBINDINGS))
    issue_types: list[dict[str, str]] = field(
        default_factory=lambda: [dict(t) for t in DEFAULT_ISSUE_TYPES]
    )
    custom_comment_key: str = "Ctrl+Return"
    # 内存回收策略：aggressive（激进）/ balanced（平衡）/ relaxed（宽松）
    memory_policy: str = DEFAULT_MEMORY_POLICY

    # -- 派生查询 ----------------------------------------------------------

    def issue_key_map(self) -> dict[str, str]:
        """问题类型名 -> 快捷键。"""
        return {t["name"]: t.get("key", "") for t in self.issue_types}

    def issue_type_names(self) -> list[str]:
        return [t["name"] for t in self.issue_types]

    def key_for_issue(self, name: str) -> str:
        return self.issue_key_map().get(name, "")

    def binding(self, action: str) -> str:
        return self.keybindings.get(action, DEFAULT_KEYBINDINGS.get(action, ""))

    def shortcut_conflicts(self) -> dict[str, list[tuple[str, str]]]:
        """当前配置里的重复快捷键，见模块级 shortcut_conflicts()。"""
        return shortcut_conflicts(
            self.keybindings, self.issue_types, self.custom_comment_key
        )

    def _heal_shortcut_conflicts(self) -> None:
        """把已知的历史撞车配置自动让位（旧版「漏字」与「红框模式」同绑 R）。

        只在旧默认值原样保留时生效：用户若已自行改绑任一侧，说明他有明确
        意图，不动。不修的话 Qt 判定歧义，两个动作都按不出来。
        """
        for item in self.issue_types:
            replacement = _LEGACY_DUPLICATE_ISSUE_KEYS.get(item.get("name", ""))
            if not replacement or not item.get("key"):
                continue
            if normalize_key(item["key"]) != "R":
                continue
            if normalize_key(self.binding("redraw_mode")) != "R":
                continue
            log.info("快捷键修复：问题类型「%s」R → %s（与红框模式撞车）",
                     item["name"], replacement)
            item["key"] = replacement

    def _migrate_issue_types(self, stored_version: int) -> None:
        """旧版问题类型表 → 新版默认键位（用户自己改过的键位保持不动）。

        - 键位仍是旧默认值的类型：跟随新表（如「漏字」R → U）；
        - 新版新增的类型：按默认位置插入，键位若被用户自定键占用则先留空；
        - 用户自己改过的键位、以及自建的类型：原样保留（自建的排在末尾）。
        """
        if stored_version >= ISSUE_TYPES_VERSION:
            return
        new_keys = {t["name"]: t["key"] for t in DEFAULT_ISSUE_TYPES}
        for item in self.issue_types:
            name = item.get("name", "")
            key = item.get("key", "")
            old_default = _LEGACY_ISSUE_KEYS.get(name)
            if old_default and normalize_key(key) == normalize_key(old_default):
                item["key"] = new_keys.get(name, key)

        current = {item["name"]: item for item in self.issue_types}
        used = {
            normalize_key(item.get("key", ""))
            for item in self.issue_types
            if item.get("key")
        }
        merged: list[dict[str, str]] = []
        for default in DEFAULT_ISSUE_TYPES:
            name = default["name"]
            if name in current:
                merged.append(current.pop(name))
                continue
            key = default["key"]
            if normalize_key(key) in used:      # 用户自定键占了这个位置 → 先留空
                key = ""
            else:
                used.add(normalize_key(key))
            merged.append({"name": name, "key": key})
        merged.extend(current.values())          # 用户自建类型保留在末尾
        self.issue_types = merged
        log.info("问题类型表已升级到 v%d（共 %d 类）", ISSUE_TYPES_VERSION, len(merged))


class SettingsManager:
    """settings.json 的读写封装。"""

    def __init__(self, path: Path | None = None):
        self._path = path if path is not None else paths.settings_path()
        self._lock = threading.Lock()
        # 本次启动时配置文件是否存在：用于「首次使用」引导（只提示，不代替决策）。
        # 注意 save() 之后文件就存在了，所以这是启动快照，不是实时状态。
        self.was_missing = not self._path.exists()
        # 旧版残留的最近打开记录（settings.json 里的 recent_paths）：
        # 只读一次，交给 RecentManager 迁移到 recent.json，见 config/recent.py
        self._legacy_recent_paths: list[str] = []
        self.settings = self._load()

    @property
    def has_settings_file(self) -> bool:
        """配置文件当前是否存在（用户确认过设置 / 程序正常退出过即存在）。"""
        return self._path.exists()

    @property
    def is_first_use(self) -> bool:
        """是否首次使用：启动时既没有 settings.json，也没有 recent.json。

        两者都没有 = 从没确认过设置、也从没打开过任务（老用户升级至少会有
        其中一个）。只用于决定要不要把设置页面直接摆到用户面前，不做任何
        预设或推荐——默认值已在代码里定好。
        """
        return self.was_missing and not self.recent_path.exists()

    @property
    def recent_path(self) -> Path:
        """最近打开记录文件：与 settings.json 同目录的独立 recent.json。"""
        return self._path.with_name(paths.RECENT_FILE_NAME)

    @property
    def legacy_recent_paths(self) -> list[str]:
        """旧版混在 settings.json 里的最近打开记录（新装程序为空）。"""
        return list(self._legacy_recent_paths)

    # -- 读写 --------------------------------------------------------------

    def _load(self) -> Settings:
        try:
            if not self._path.exists():
                return Settings()
            with open(self._path, "r", encoding="utf-8") as f:
                raw: dict[str, Any] = json.load(f)
            return self._from_dict(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log.warning("读取 settings.json 失败，使用默认设置：%s", exc)
            return Settings()

    def _from_dict(self, raw: dict[str, Any]) -> Settings:
        s = Settings()

        ratio = raw.get("layer_display_ratio", DEFAULT_DISPLAY_RATIO)
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            ratio = DEFAULT_DISPLAY_RATIO
        if not (0.01 <= ratio <= 4.0):
            ratio = DEFAULT_DISPLAY_RATIO
        s.layer_display_ratio = ratio

        mode = raw.get("compare_mode", DEFAULT_COMPARE_MODE)
        s.compare_mode = mode if mode in ("auto", "manual") else DEFAULT_COMPARE_MODE

        try:
            hz = int(raw.get("compare_speed_hz", DEFAULT_COMPARE_SPEED_HZ))
        except (TypeError, ValueError):
            hz = DEFAULT_COMPARE_SPEED_HZ
        if not (1 <= hz <= 10):
            hz = DEFAULT_COMPARE_SPEED_HZ
        s.compare_speed_hz = hz

        wheel = raw.get("wheel_mode", DEFAULT_WHEEL_MODE)
        s.wheel_mode = wheel if wheel in ("pan", "zoom") else DEFAULT_WHEEL_MODE

        scope = raw.get("issue_scope", DEFAULT_ISSUE_SCOPE)
        s.issue_scope = scope if scope in ISSUE_SCOPES else DEFAULT_ISSUE_SCOPE

        s.show_layer_outline = bool(
            raw.get("show_layer_outline", DEFAULT_SHOW_LAYER_OUTLINE)
        )
        s.continuous_annotation = bool(
            raw.get("continuous_annotation", DEFAULT_CONTINUOUS_ANNOTATION)
        )

        s.recursive_scan = bool(raw.get("recursive_scan", False))
        s.generate_pdf_on_complete = bool(
            raw.get("generate_pdf_on_complete", True)
        )
        s.report_name = str(raw.get("report_name", "") or "")

        fmt = raw.get("report_image_format", DEFAULT_REPORT_IMAGE_FORMAT)
        s.report_image_format = (
            fmt if fmt in REPORT_IMAGE_FORMATS else DEFAULT_REPORT_IMAGE_FORMAT
        )
        try:
            quality = int(raw.get("report_jpeg_quality", DEFAULT_JPEG_QUALITY))
        except (TypeError, ValueError):
            quality = DEFAULT_JPEG_QUALITY
        if not (60 <= quality <= 95):
            quality = DEFAULT_JPEG_QUALITY
        s.report_jpeg_quality = quality
        s.report_hide_clean_files = bool(raw.get("report_hide_clean_files", True))
        s.hide_console = bool(raw.get("hide_console", True))
        s.custom_comment_key = str(
            raw.get("custom_comment_key", DEFAULT_KEYBINDINGS["custom_comment"])
        )

        kb = raw.get("keybindings", {})
        if isinstance(kb, dict):
            merged = dict(DEFAULT_KEYBINDINGS)
            for k, v in kb.items():
                if isinstance(v, str) and v.strip():
                    merged[k] = v.strip()
            s.keybindings = merged

        types = raw.get("issue_types")
        if isinstance(types, list) and types:
            cleaned = []
            seen = set()
            for item in types:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                cleaned.append({"name": name, "key": str(item.get("key", ""))})
            if cleaned:
                s.issue_types = cleaned

        try:
            types_version = int(raw.get("issue_types_version", 1))
        except (TypeError, ValueError):
            types_version = 1
        s._migrate_issue_types(types_version)
        s._heal_shortcut_conflicts()

        # 兼容旧版：最近打开原本混存在 settings.json（现为独立 recent.json）。
        # 这里只读不写——迁移由 config/recent.py 完成，之后该键自然消失。
        recent = raw.get("recent_paths", [])
        if isinstance(recent, list):
            self._legacy_recent_paths = [
                str(p) for p in recent if isinstance(p, str) and p.strip()
            ][:10]

        policy = raw.get("memory_policy", DEFAULT_MEMORY_POLICY)
        s.memory_policy = policy if policy in MEMORY_POLICIES else DEFAULT_MEMORY_POLICY

        return s

    def save(self) -> None:
        with self._lock:
            try:
                payload = {
                    "settings_version": SETTINGS_VERSION,
                    "layer_display_ratio": self.settings.layer_display_ratio,
                    "compare_mode": self.settings.compare_mode,
                    "compare_speed_hz": self.settings.compare_speed_hz,
                    "wheel_mode": self.settings.wheel_mode,
                    "issue_scope": self.settings.issue_scope,
                    "show_layer_outline": self.settings.show_layer_outline,
                    "continuous_annotation": self.settings.continuous_annotation,
                    "recursive_scan": self.settings.recursive_scan,
                    "generate_pdf_on_complete": self.settings.generate_pdf_on_complete,
                    "report_name": self.settings.report_name,
                    "report_image_format": self.settings.report_image_format,
                    "report_jpeg_quality": self.settings.report_jpeg_quality,
                    "report_hide_clean_files": self.settings.report_hide_clean_files,
                    "hide_console": self.settings.hide_console,
                    "custom_comment_key": self.settings.custom_comment_key,
                    "keybindings": self.settings.keybindings,
                    "issue_types": self.settings.issue_types,
                    "issue_types_version": ISSUE_TYPES_VERSION,
                    "memory_policy": self.settings.memory_policy,
                }
                tmp = self._path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                tmp.replace(self._path)
            except OSError as exc:
                log.warning("写入 settings.json 失败：%s", exc)
