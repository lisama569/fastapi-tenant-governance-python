# 多租户访问治理服务（FastAPI）

在原有 FastAPI 骨架上补充多租户访问治理层：**租户识别与数据隔离、按租户固定窗配额、写操作幂等重试、全程审计可查**。
所有状态（租户、账户、配额计数、幂等记录）持久化在本地 SQLite，服务重启后行为可预期。

## 1. 租户模型

- 每个请求必须携带 `X-API-Key` 头，凭据形如 `tk_<tenant_id>.<secret>`，例如
  `tk_tenant-a.S2u9Vq7XmK1pR4wL`。
- 数据库只保存凭据的 SHA-256 哈希；校验用常量时间比较（`hmac.compare_digest`）。
- 租户可配置 **配额上限**、**启用/停用**、**过期时间**（见 `app/config.py`）。

### 认证失败的三种可区分原因（均为 HTTP 401，用 `error.code` 区分）

| 场景 | code |
| --- | --- |
| 未带 `X-API-Key` | `MISSING_CREDENTIALS` |
| 凭据格式错误 / 租户不存在 / secret 不匹配（伪造） | `FORGED_CREDENTIAL` |
| 凭据为真，但租户被停用或 Key 已过期（失效） | `INVALID_CREDENTIALS` |

内置租户（仅用于本地/测试，生产应由密钥管理系统下发）：

| tenant_id | 每窗配额 | 状态 |
| --- | --- | --- |
| `tenant-a` | 5 | 启用 |
| `tenant-b` | 3 | 启用 |
| `tenant-disabled` | 2 | 停用（演示“失效”） |

### 数据隔离

- 账户以全局唯一的 `account_id` 标识，归属某个 `tenant_id`。
- 每次读/写前先查资源 owner：
  - owner 与调用租户一致 → 放行；
  - owner 是别的租户 → **403 `CROSS_TENANT_DENIED`**（即使已拿到对方资源标识）；
  - 资源不存在 → **404 `RESOURCE_NOT_FOUND`**（不向他租户泄露资源存在性）。
- 用别的租户已占用的 `account_id` 开户，同样按 403 越权拒绝。

## 2. 额度计算规则

- 采用**固定时间窗**计数：默认窗口 60 秒，可用环境变量 `TENANT_QUOTA_WINDOW` 调整。
- 窗口起点 `window_start = floor(now / window) * window`；每个 `(租户, 窗口)` 一行计数。
- 计数在 SQLite 单事务内 `BEGIN IMMEDIATE` 读改写，并由进程锁串行化，
  因此**并发请求不会把同一窗口的计数加多**（有测试：10 并发精确只放行剩余额度）。
- 窗口结束后 `window_start` 变化，新窗口计数从 0 开始，**自动恢复**，无需人工干预。
- 超限时返回 **429 `QUOTA_EXCEEDED`**，响应体给出明确的恢复等待时间：

  ```json
  {"error": {"code": "QUOTA_EXCEEDED", "status_code": 429,
             "detail": {"limit": 5, "window_seconds": 60,
                        "window_count": 5, "retry_after_seconds": 42}}}
  ```

- 每个写请求（开户/存款）占用一个配额；**幂等重放不占用配额**（见下）。
- 注意与越权结论的区别：超限是 **429 `QUOTA_EXCEEDED`**，越权是 **403 `CROSS_TENANT_DENIED`**。

## 3. 幂等约定

- 所有写请求（`POST /accounts`、`POST /accounts/deposit`）必须带 `Idempotency-Key` 头，
  缺失返回 **400 `MISSING_IDEMPOTENCY_KEY`**。
- 幂等键的作用域为**租户内**：`(tenant_id, idem_key)` 为主键。
- 请求体的 SHA-256 与键绑定：
  - **同键 + 同体**（包括并发重放）：只有首个请求真正落账，其余请求返回
    **首次响应的完整内容与相同状态码**（含余额快照），响应体带 `"replayed": true`；
  - **同键 + 异体**：返回 **409 `IDEMPOTENCY_CONFLICT`**，第二个请求绝不落账。
- 并发保证：主键插入只有一个“首任者”成功；同键同体的并发请求在进程内
  Condition 上等待首任完成后取同一结果（跨进程则轮询重读），不会重复落账或状态错乱。
- 首任者业务失败（如 404/403/422）时清除 IN_FLIGHT 登记，**同键可安全重试**；错误不缓存。

### 服务重启后的一致行为

- 配额计数与已完成的幂等结果都落盘：重启后**额度延续**、同键重放仍返回首次结果、
  跨租户隔离关系保留。
- 若上次进程在业务执行中途崩溃（留下未落结果的 IN_FLIGHT 记录），启动时会删除这些
  残留行——因为那些请求从未生效，客户端可用同一个键重新提交，语义等同首次请求。

## 4. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（无需认证） |
| POST | `/accounts` | 开户（幂等写，body: `{"account_id": "..."}`） |
| POST | `/accounts/deposit` | 存款（幂等写，body: `{"account_id": "...", "amount": 100}`） |
| GET | `/accounts/{account_id}` | 查询余额（租户隔离） |

错误响应统一形如：

```json
{"error": {"code": "QUOTA_EXCEEDED", "status_code": 429,
           "message": "...", "detail": {...}}}
```

### curl 示例

```bash
# 开户
curl -s -X POST http://127.0.0.1:8000/accounts \
  -H 'X-API-Key: tk_tenant-a.S2u9Vq7XmK1pR4wL' \
  -H 'Idempotency-Key: acc-create-001' \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"acc-001"}'

# 存款（同键同体重试返回首次结果，不会重复入账）
curl -s -X POST http://127.0.0.1:8000/accounts/deposit \
  -H 'X-API-Key: tk_tenant-a.S2u9Vq7XmK1pR4wL' \
  -H 'Idempotency-Key: dep-001' \
  -H 'Content-Type: application/json' \
  -d '{"account_id":"acc-001","amount":100}'
```

## 5. 审计日志

每次治理判定都会向标准输出打印一行 JSON（`app/audit.py`），包含
**租户、动作、结论（ALLOW/DENY/REPLAY）、判定依据 reason、请求输入**，
例如：

```json
AUDIT {"tenant_id":"tenant-b","action":"access","decision":"DENY",
       "reason":"cross_tenant_write_denied","account_id":"acc-001","owner":"tenant-a"}
```

被拦下时可据此回答“是谁（tenant_id）、因为什么（reason）被拦下”。
跑单测加 `-s` 即可在测试日志中看到请求输入与判定依据。

## 6. 本地验证

```bash
# 安装依赖（含 dev：pytest/httpx）
uv sync --group dev

# 跑全部单测（-s 打印请求输入与判定依据）
uv run pytest -s

# 启动服务（默认 SQLite 文件 tenant_gov.db，可用 TENANT_GOV_DB 覆盖）
uv run python main.py
# 自定义配额窗口：TENANT_QUOTA_WINDOW=10 uv run python main.py
```

测试覆盖三类重点场景：

- `tests/test_idempotency_concurrency.py`：20 线程同键并发只落一次账、同键异体 409；
- `tests/test_quota.py`：固定窗边界（恰好放行/第 N+1 拒绝）、并发不多放、
  窗口滚动自动恢复、`retry_after_seconds`、重放不耗额；
- `tests/test_tenant_isolation.py`：持有对方资源标识读/写/抢占均 403，403 与 429 可区分；
- 另含 `tests/test_auth.py`（缺失/伪造/失效三分）与
  `tests/test_restart_persistence.py`（重启后额度、幂等、崩溃残留的一致行为）。

## 代码结构

```
app/
  config.py       租户/配额/数据库路径配置
  errors.py       错误码与 HTTP 状态（401/403/404/409/429）
  audit.py        结构化审计日志
  store.py        SQLite 持久层（账户、原子配额计数、幂等状态机）
  auth.py         凭据解析与三种失败原因
  quota.py        固定窗配额检查
  idempotency.py  幂等并发控制（首任者/重放/冲突/失败重试）
  schemas.py      请求响应模型
  deps.py         运行期设置与认证依赖
  routes.py       /accounts、/accounts/deposit、健康检查
  main.py         应用装配与统一异常处理
tests/            并发幂等、配额边界、跨租户越权、认证、重启一致性
```
