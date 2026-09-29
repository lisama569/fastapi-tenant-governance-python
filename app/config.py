"""运行配置：租户凭据、配额窗口与 SQLite 路径。

凭据形如 ``tk_<tenant_id>.<secret>``：
- 结构/密钥可验证为真，但租户停用或凭据过期 -> 失效（INVALID_CREDENTIALS）；
- 结构错误、租户不存在或密钥不匹配 -> 伪造（FORGED_CREDENTIAL）。
数据库只保存凭据的 SHA-256 哈希，不存明文。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from app.auth import hash_api_key

#: 固定时间窗长度（秒），可用环境变量覆盖
QUOTA_WINDOW_SECONDS: int = int(os.environ.get("TENANT_QUOTA_WINDOW", "60"))

#: SQLite 数据库文件路径
DB_PATH: str = os.environ.get("TENANT_GOV_DB", "tenant_gov.db")


@dataclass(frozen=True)
class TenantConfig:
    """单个租户的静态配置（凭据、额度、启用状态）。"""

    tenant_id: str
    api_key: str
    quota_limit: int
    enabled: bool = True
    expires_at: float | None = None  # Unix epoch 秒；None 表示永不过期

    @property
    def api_key_hash(self) -> str:
        return hash_api_key(self.api_key)

    def is_active(self, now: float | None = None) -> bool:
        """凭据当前是否有效：租户启用且未过期。"""
        if not self.enabled:
            return False
        if self.expires_at is not None and (now or time.time()) >= self.expires_at:
            return False
        return True


#: 内置租户（本地开发与测试使用；生产应由密钥管理系统下发）
DEFAULT_TENANTS: list[TenantConfig] = [
    TenantConfig(tenant_id="tenant-a", api_key="tk_tenant-a.S2u9Vq7XmK1pR4wL", quota_limit=5),
    TenantConfig(tenant_id="tenant-b", api_key="tk_tenant-b.H8nT3yB6eJ0cF5xA", quota_limit=3),
    # 密钥本身正确，但租户已被停用 -> 用于演示“失效”
    TenantConfig(
        tenant_id="tenant-disabled",
        api_key="tk_tenant-disabled.Q4zD7vN2bG9hW6kE",
        quota_limit=2,
        enabled=False,
    ),
]


def default_tenants() -> list[TenantConfig]:
    """返回内置租户清单的副本。"""
    return list(DEFAULT_TENANTS)
