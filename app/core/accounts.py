"""Gemini 账号元数据（非密钥）：account_id → name / email / project_id。

设计要点：
* **密钥不在本模块**。密钥仍只存在 `.env` 的 `GEMINI_KEYS`（不入库），
  本模块只维护可公开的身份信息，方便 CLI 用 `--account 2` 直接定位账号。
* `.env` 里的 `GEMINI_KEYS` 用 `account_id|key` 标识；旧的 `名称|key` 仍然兼容
  （key_manager 会先按 id 查，查不到再当名字用）。
* 三个 Key 分属**三个独立的 Google 账号**，各自有独立项目与独立配额，
  因此按 (key, model) 建令牌桶是对的，不需要做项目级聚合。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

# 内置默认值：即使 .env 没配 GEMINI_ACCOUNTS，也能直接用 --account 1/2/3
DEFAULT_ACCOUNTS = (
    {
        "id": 1,
        "name": "qin",
        "email": "qin16778@gmail.com",
        "project_id": "gen-lang-client-0386075480",
    },
    {
        "id": 2,
        "name": "edis",
        "email": "edis.mujcic@hotmail.com",
        "project_id": "gen-lang-client-0059371429",
    },
    {
        "id": 3,
        "name": "sheron",
        "email": "loreneesporoconn@gmail.com",
        "project_id": "gen-lang-client-0484613085",
    },
)


def load_accounts() -> Dict[int, Dict[str, Any]]:
    """返回 {account_id: {id, name, email, project_id}}。

    `.env` 的 GEMINI_ACCOUNTS 为 JSON 数组时优先使用，否则用内置默认值。
    """
    raw = (os.environ.get("GEMINI_ACCOUNTS") or "").strip()
    items: Any = []
    if raw:
        try:
            items = json.loads(raw)
        except Exception:
            items = []
    if not isinstance(items, list) or not items:
        items = list(DEFAULT_ACCOUNTS)

    accounts: Dict[int, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            acc_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        accounts[acc_id] = {
            "id": acc_id,
            "name": str(item.get("name") or "").strip(),
            "email": str(item.get("email") or "").strip(),
            "project_id": str(item.get("project_id") or "").strip(),
        }
    return accounts


def account_by_ident(ident: str) -> Optional[Dict[str, Any]]:
    """按标识取账号：支持 account_id（"2"）、名字（"edis"）或邮箱。"""
    accounts = load_accounts()
    text = (ident or "").strip()
    if not text:
        return None
    if text.isdigit():
        return accounts.get(int(text))
    low = text.lower()
    for acc in accounts.values():
        if acc["name"].lower() == low or acc["email"].lower() == low:
            return acc
    return None


def name_for_id(acc_id: int) -> str:
    """account_id → 展示名；查不到时返回 `account-<id>`。"""
    acc = load_accounts().get(acc_id)
    return acc["name"] if acc and acc["name"] else f"account-{acc_id}"


def project_for_ident(ident: str) -> str:
    """account_id / 名字 / 邮箱 → project_id；查不到返回空串。"""
    acc = account_by_ident(ident)
    return acc["project_id"] if acc else ""
