# SPDX-FileCopyrightText: 2026 gunfub
# SPDX-License-Identifier: GPL-3.0-only

"""MirrorChyan CDK 的存储与迁移（需求 §14、§15）。

存储策略：

- **桌面端**：优先系统凭据库（keyring），逻辑命名固定为
  ``service = MangaProof`` / ``username = mirrorchyan_cdk``；
  但**不能只判断 import 是否成功** —— 必须实测当前 backend 能否
  ``set`` / ``get`` / ``delete`` 成功（无桌面会话的 Linux、纯 SSH 环境里
  keyring 能 import 却用不了）；
- **失败回退**：写 ``settings.json`` 的 ``update.mirrorchyan_cdk`` 明文；
- **Android**：不打包 keyring（需求 §14），直接存 settings.json
  （应用私有目录，只有应用自己可读）。

迁移（需求 §15）：桌面端若发现 settings.json 里有明文 CDK 且 keyring 可用，
则 写入 keyring → 重新读取验证 → 删除明文。keyring 不可用时**保留明文**，
且**绝不因为 keyring 失败而禁用更新功能**。

安全（需求 §16）：CDK 不进日志。本模块所有日志只输出"存到哪了"，
不输出值本身；异常文本一律过 :func:`mangaproof.update.redact.redact_text`。
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from mangaproof.utils.platform import is_android_strict

log = logging.getLogger("mangaproof.update.cdk_store")

#: keyring 的逻辑命名（需求 §14：逻辑命名必须固定）
KEYRING_SERVICE = "MangaProof"
KEYRING_USERNAME = "mirrorchyan_cdk"

STORED_IN_KEYRING = "keyring"
STORED_IN_SETTINGS = "settings.json"

#: 探测 keyring 可用性时用的临时键（用完立即删除，不污染用户凭据）
_PROBE_USERNAME = "__mangaproof_probe__"


def _keyring_module() -> Any | None:
    """导入 keyring；Android 上直接返回 ``None``（需求 §14：不打包 keyring）。"""
    if is_android_strict():
        return None
    try:
        import keyring  # noqa: PLC0415 —— 桌面专有依赖，必须延迟导入

        return keyring
    except Exception:
        log.info("keyring 不可用（未安装或导入失败），CDK 将存于 settings.json")
        return None


def keyring_available() -> bool:
    """实测 keyring 是否**真的可用**（需求 §14：不能只看 import）。

    做法：用随机值做一次完整的 set → get → delete。
    任何一步失败都判为不可用；探测键用完即删，不留残留。
    """
    module = _keyring_module()
    if module is None:
        return False
    probe_value = secrets.token_hex(8)
    try:
        module.set_password(_PROBE_USERNAME, _PROBE_USERNAME, probe_value)
        if module.get_password(_PROBE_USERNAME, _PROBE_USERNAME) != probe_value:
            return False
        return True
    except Exception as exc:
        log.info("keyring 后端不可用，回退到 settings.json：%s", type(exc).__name__)
        return False
    finally:
        try:
            module.delete_password(_PROBE_USERNAME, _PROBE_USERNAME)
        except Exception:
            pass


def load_cdk(update_settings: Any) -> str:
    """读取 CDK：keyring 优先，其次 settings.json 明文（需求 §14）。"""
    module = _keyring_module()
    if module is not None:
        try:
            value = module.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            if value:
                return str(value)
        except Exception as exc:
            log.info("从 keyring 读取 CDK 失败：%s", type(exc).__name__)
    return str(getattr(update_settings, "cdk", "") or "")


def save_cdk(update_settings: Any, value: str) -> str:
    """保存 CDK，返回实际落点（``keyring`` 或 ``settings.json``）。

    桌面端写入 keyring 成功时，**清空** settings.json 里的明文；
    失败则写明文（需求 §14：不得因 keyring 失败而禁用更新功能）。
    """
    value = (value or "").strip()
    module = _keyring_module()
    if module is not None and value:
        try:
            module.set_password(KEYRING_SERVICE, KEYRING_USERNAME, value)
            if module.get_password(KEYRING_SERVICE, KEYRING_USERNAME) == value:
                update_settings.cdk = ""
                log.info("CDK 已保存到系统凭据库（keyring）")
                return STORED_IN_KEYRING
        except Exception as exc:
            log.info("写入 keyring 失败，回退 settings.json：%s", type(exc).__name__)

    update_settings.cdk = value
    log.info("CDK 已保存到 settings.json%s",
             "（Android：应用私有目录）" if is_android_strict() else "（明文，keyring 不可用）")
    return STORED_IN_SETTINGS


def clear_cdk(update_settings: Any) -> None:
    """清除 CDK（两处都清）。"""
    module = _keyring_module()
    if module is not None:
        try:
            module.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            pass
    update_settings.cdk = ""


def migrate_plaintext_cdk(update_settings: Any) -> bool:
    """需求 §15：settings.json 明文 CDK → keyring → 验证 → 删除明文。

    :returns: 是否真的完成了迁移（``False`` 表示无需迁移或 keyring 不可用）。
    """
    plaintext = str(getattr(update_settings, "cdk", "") or "").strip()
    if not plaintext or is_android_strict():
        return False
    if not keyring_available():
        log.info("keyring 不可用，保留 settings.json 中的明文 CDK")
        return False

    module = _keyring_module()
    assert module is not None  # keyring_available() 已确认
    try:
        module.set_password(KEYRING_SERVICE, KEYRING_USERNAME, plaintext)
        if module.get_password(KEYRING_SERVICE, KEYRING_USERNAME) != plaintext:
            log.warning("CDK 迁移失败（回读不一致），保留明文")
            return False
    except Exception as exc:
        log.warning("CDK 迁移失败（%s），保留明文", type(exc).__name__)
        return False

    update_settings.cdk = ""
    log.info("CDK 已从 settings.json 迁移到系统凭据库，并删除明文")
    return True
