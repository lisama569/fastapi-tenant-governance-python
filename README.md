# 多租户访问治理服务

在原有 FastAPI 服务之上补充了多租户访问治理层：租户识别与数据隔离、按租户配额限流、
写操作幂等与重试、全链路审计日志。

## 租户模型

- 每个租户拥有一条独立凭据（API Key），通过请求头 `X-Api-Key` 携带。
- 租户凭据有三种可区分的失败（均返回 401，`error.code` 不同）：

| 场景 | error.code |
| --- | --- |
| 未携带 `X-Api-Key` | `TENANT_CREDENTIAL_MISSING` |
| 凭据不存在 / 伪造 | `TENANT_CREDENTIAL_INVALID` |
| 凭据已被吊销 | `TENANT_CREDENTIAL_REVOKED` |

- **数据隔离**：所有资源以 `(tenant_id, resource_id)` 为主键存储，查询强制带当前租户标识。
  即使拿到其他租户的资源 ID，跨租户访问一律返回 `404 RESOURCE_NOT_FOUND`，不泄露资源归属。
- 每次拒绝都会写入审计日志（`tenant_governance` logger 与 `service.audit_log`），
  记录是谁（tenant_id）、因为什么（reason + code）被拦下。

## 额度计算规则

- 按租户独立计数，读写请求均消耗额度，限额存在租户配置（`quota_limit`）中。
- **固定窗口**：窗口按 UNIX 时间整除对齐（`WINDOW_SECONDS = 60` 秒），
  窗口内计数在 SQLite 事务 + 进程锁下原子递增，并发突发不会多放额度。
- 超限返回 `429`，`error.code = QUOTA_EXCEEDED`，并携带 `Retry-After` 响应头
  （距离下一窗口的秒数），窗口切换后计数自动归零、自动恢复。

## 幂等约定

- 写操作（`POST /documents`）必须携带 `Idempotency-Key` 请求头，缺失返回
  `400 IDEMPOTENCY_KEY_MISSING`。
- 同一租户 + 同一键 + 相同请求体：只生效一次，重复提交（含并发重放）返回与首次一致的结果，
  且不重复消耗配额。并发重放会阻塞等待首个请求完成后重放其结果。
- 同一租户 + 同一键 + 不同请求体：返回 `409 IDEMPOTENCY_CONFLICT`。
- 幂等键按租户隔离，不同租户使用相同的键互不影响。
- 首个请求若因超限等原因失败，键会被释放，窗口恢复后重试可正常执行。

## 重启后的一致行为

存储使用 SQLite（默认内存库，生产可配置为文件库）。使用文件库时：

- 配额窗口计数保留：重启前已消耗的额度在重启后仍然有效；
- 幂等记录保留：重启后重复提交仍重放首次结果；
- 上述行为由 `tests/test_quota.py::test_state_survives_restart` 验证。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/documents` | 创建文档（需 `X-Api-Key` + `Idempotency-Key`） |
| GET | `/documents/{doc_id}` | 读取文档（需 `X-Api-Key`，跨租户返回 404） |

错误响应统一为 `{"error": {"code": ..., "detail": ...}}`。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（并发重复提交 / 配额边界 / 跨租户越权）
uv run pytest tests/ -q

# 查看测试中的请求输入与判定依据
uv run pytest tests/ -q -s

# 启动服务
uv run python main.py
```

测试覆盖三类关键场景：

- `tests/test_concurrent_idempotency.py`：并发重复提交只生效一次、冲突唯一胜者、顺序重放结果一致；
- `tests/test_quota.py`：配额精确边界、并发突发不超放、窗口自动恢复、租户额度隔离、重启后状态延续；
- `tests/test_tenant_isolation.py`：凭据失败原因可区分、跨租户 404、越权与超限结论可区分、幂等键租户隔离。
