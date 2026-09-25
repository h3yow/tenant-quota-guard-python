# tenant-quota-guard-python

为现有 FastAPI 服务提供多租户请求配额治理层：租户身份校验、固定窗口配额计数、
超限拦截、治理组件异常时的显式降级，以及按租户隔离的审计记录。

## 租户识别

每个请求（`/health` 除外）必须携带租户身份，支持两个来源：

- 请求头：`X-Tenant-Id: <tenant-id>`
- 查询参数：`?tenant_id=<tenant-id>`

格式要求：小写字母开头，仅含小写字母、数字、中划线，长度 3~32
（正则 `^[a-z][a-z0-9-]{2,31}$`）。两个来源同时存在时必须一致。

| 场景 | HTTP 状态 | reason |
| --- | --- | --- |
| 身份缺失 | 401 | `tenant_missing` |
| 格式非法 | 400 | `tenant_invalid` |
| 头与查询参数冲突 | 409 | `tenant_conflict` |
| 超出配额 | 429 | `quota_exceeded`（携带 `Retry-After` 响应头） |
| 计数后端不可用（fail-closed） | 503 | `quota_unavailable` |

错误响应体形如 `{"error": {"reason": "...", "retry_after": 9.0}}`，
不包含内部堆栈或任何其他租户的信息。

## 配额与窗口语义

- 固定窗口计数：窗口起点按 `window_seconds` 对齐，
  即 `window_start = floor(now / window_seconds) * window_seconds`。
- 进入新窗口时该租户的计数自动重置，语义确定、可复现（测试中以注入的 `now` 验证）。
- 默认每租户每 60 秒 100 次请求（见 `main.py` 中 `QuotaCounter(limit=100, window_seconds=60.0)`）。
- 计数按租户分桶、互不影响；同一租户并发突发通过锁保证不超放。
- 超限响应携带 `Retry-After`（距下一窗口的秒数）。

## 异常降级策略

治理组件内部失败时按显式策略处理，绝不静默吞异常：

- **计数后端不可用**（`QuotaStoreUnavailable`）：按 `DegradationPolicy` 处理——
  `FAIL_CLOSED`（默认）拒绝并返回 503 `quota_unavailable`；
  `FAIL_OPEN` 放行并标记 `degraded`。两种路径都会写入审计记录作为判定依据。
- **审计写入失败**：不影响本次判定结果，但判定标记 `degraded=True`
  （HTTP 层以 `X-Quota-Degraded: true` 响应头体现），并记录错误日志。
- **未知内部异常**：兜底转为 503 `quota_unavailable`，不向外暴露堆栈或跨租户信息。

## 审计

每次判定（放行/拒绝/降级）都会生成一条 `AuditRecord`
（租户、判定结果、原因、明细），按租户隔离存储，
只能通过 `records_for(tenant_id)` 查询本租户记录。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（身份、窗口、并发抢占、降级、端到端）
uv run pytest -q

# 查看测试打印的请求输入与判定依据
uv run pytest -s

# 启动服务
uv run python main.py   # 监听 127.0.0.1 随机端口

# 手动验证示例
curl -i http://127.0.0.1:<port>/ -H 'X-Tenant-Id: tenant-a'
curl -i http://127.0.0.1:<port>/                      # 401 tenant_missing
```

## 代码结构

- `app/identity.py` — 租户身份解析与校验（缺失/非法/冲突可区分）
- `app/quota.py` — 固定窗口配额计数器（按租户隔离、并发安全）
- `app/audit.py` — 按租户隔离的审计记录
- `app/guard.py` — 编排层：配额判定 + 审计 + 降级策略
- `app/errors.py` — 可区分的拒绝原因与错误类型
- `main.py` — FastAPI 中间件挂载与错误响应映射
- `tests/` — 身份、配额、降级、端到端并发测试
