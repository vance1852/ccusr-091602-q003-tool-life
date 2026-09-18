"""人工放行/复核签字：HMAC-SHA256 绑定操作范围与用途。

演示系统采用对称密钥目录（按签字人登记密钥）。生产环境可替换为
非对称证书验签，接口 ``sign_scope`` / ``verify_scope_signature`` 不变。
签名明文为规范化 JSON（字段排序、无空白），杜绝“同字不同串”。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Optional

from .domain import InvalidSignature, PermissionDenied


class KeyDirectory:
    """签字人 -> HMAC 密钥。"""

    def __init__(self, keys: Optional[dict[str, str]] = None):
        self._keys: dict[str, bytes] = {
            user: secret.encode("utf-8") for user, secret in (keys or {}).items()
        }

    def register(self, user: str, secret: str) -> None:
        self._keys[user] = secret.encode("utf-8")

    def key_for(self, user: str) -> bytes:
        if user not in self._keys:
            raise PermissionDenied(f"签字人未登记密钥：{user}")
        return self._keys[user]

    def has(self, user: str) -> bool:
        return user in self._keys

    @staticmethod
    def from_dir(path: str) -> "KeyDirectory":
        """目录下每个文件 ``<用户名>.key`` 内容为密钥。"""
        kd = KeyDirectory()
        for f in Path(path).glob("*.key"):
            kd.register(f.stem, f.read_text(encoding="utf-8").strip())
        return kd


def canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def signing_body(*, purpose: str, scope: dict, **extra) -> dict:
    body = {"purpose": purpose, "scope": _scoped(scope)}
    body.update(extra)
    return body


def override_body(override_id: str, scope: dict, reason: str,
                  valid_until: str) -> dict:
    """人工放行单的规范签字明文（服务层签发与重放验签共用）。"""
    return {
        "purpose": "manual_override",
        "override_id": override_id,
        "scope": _scoped(scope),
        "reason": reason,
        "valid_until": valid_until,
    }


def review_body(review_id: str, decision: str, note: str) -> dict:
    """迟到回执复核结论的规范签字明文。"""
    return {
        "purpose": "late_report_review",
        "review_id": review_id,
        "decision": decision,
        "note": note or "",
    }


def _scoped(scope: dict) -> dict:
    # 统一排序键，避免调用方构造顺序影响签名
    return dict(sorted(scope.items()))


def sign_scope(keys: KeyDirectory, user: str, body: dict) -> str:
    """对完整签字体签名。body 必须含 purpose/scope。"""
    msg = canonical(body).encode("utf-8")
    return hmac.new(keys.key_for(user), msg, hashlib.sha256).hexdigest()


def verify_scope_signature(keys: KeyDirectory, user: str, signature: str,
                           body: dict) -> None:
    """验签；``body`` 是落库事件中除签名字段外的可重放签字体。"""
    expected = sign_scope(keys, user, body)
    if not hmac.compare_digest(expected, signature or ""):
        raise InvalidSignature(f"{user} 的签字与范围不符（可能被越权改写）")
