# tenant-quota-guard-python

为现有 FastAPI 服务提供多租户请求配额治理层：租户身份校验、按租户可配置的
固定窗口配额计数、超限拦截、治理后端任意故障下的显式降级、按租户隔离的
审计记录，以及历史租户状态的可控回收。

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
| 计数后端故障（fail-closed） | 503 | `quota_unavailable`（携带 `Retry-After` 响应头） |

错误响应体形如 `{"error": {"reason": "...", "retry_after": 9.0}}`，
不包含内部堆栈或任何其他租户的信息。

## 配额配置：默认值 + 按租户覆盖

所有配额参数集中在 `GuardConfig`（见 `app/config.py`），服务启动时
（`main.py` 导入期）即完成全部校验：**任何非法配置都会让服务启动失败**，
不会等请求进入才暴露。校验项包括：上限/窗口/重试提示必须为正数、
降级策略必须是合法枚举、覆盖键必须是合法租户 ID、覆盖值必须是
`TenantQuotaConfig`。

- 默认值：`default_limit` / `default_window_seconds` / `default_policy`
  / `unavailable_retry_after`，对所有租户生效。
- 按租户覆盖：`tenant_overrides` 中为指定租户登记 `TenantQuotaConfig`
  （上限、窗口、可选的降级策略）。未登记的租户、或覆盖中未指定的策略，
  一律回落到默认值。
- 登记方式：编辑 `main.py` 中的 `TENANT_OVERRIDES` 字典后重启服务。

窗口语义（确定且可复现）：

- 固定窗口计数：窗口起点按**该租户生效的** `window_seconds` 对齐，
  即 `window_start = floor(now / window_seconds) * window_seconds`。
- 进入新窗口时该租户的计数自动重置。
- 计数按租户分桶、互不影响；同一租户在其生效窗口内并发突发通过锁保证不超放。
- 超限响应携带 `Retry-After`（距该租户下一窗口的秒数）。

## 异常降级策略

计数后端以**任意方式**失败——显式不可用（`QuotaStoreUnavailable`）、
连接失败（`ConnectionError`）、响应超时（`TimeoutError`）、未预料的运行
错误（`RuntimeError` 等）——治理层统一捕获，按**该租户生效的**
`DegradationPolicy` 处理：

- `FAIL_CLOSED`（默认）：拒绝，响应 503 `quota_unavailable`，并携带
  `Retry-After`（取 `unavailable_retry_after`，默认 1 秒）。
- `FAIL_OPEN`：放行，响应正常返回但带 `X-Quota-Degraded: true` 响应头。

判定与对外响应共用同一个 `GuardDecision`，调用方看到的放行/拒绝结果
与策略语义严格一致；治理层自身出现未预料异常时，兜底路径同样按生效
策略给出判定，不会绕过策略。所有降级判定都会写入审计，明细中包含
后端的失败方式（异常类型），可追溯。

其他内部失败：

- **审计写入失败**：不影响本次判定结果，但判定标记 `degraded=True`
  （HTTP 层以 `X-Quota-Degraded: true` 体现），并记录错误日志，绝不静默吞异常。

## 审计

每次判定（放行/拒绝/降级）都会生成一条 `AuditRecord`
（租户、判定结果、原因、明细），按租户隔离存储，
只能通过 `records_for(tenant_id)` 查询本租户记录。

**身份校验被拒的请求同样留痕**：按请求中可解析出的合法租户 ID 候选分别
记录（如头与查询参数冲突时，两个候选租户各自留下一条 `tenant_conflict`
记录）；一个合法 ID 都解析不出时（缺失或格式非法），归入保留键
`<unidentified>`（该值不是合法租户 ID，不会与真实租户冲突）。
任何拒绝都有可追溯依据，且记录严格按租户隔离、互不可见。

## 状态回收

服务长期运行、租户不断增删时，计数状态不会无限堆积：

- `QuotaCounter.reclaim_idle(now) -> list[str]`：清理**窗口已过期**的
  租户桶，返回被清理的租户列表。过期桶对后续判定没有任何影响（新请求
  落入新窗口本就会重置计数），且回收与 `try_acquire` 在同一把锁下互斥，
  因此回收与正常请求并发进行时结果一致，窗口边界附近不会漏计或误判，
  也不触及其他租户。适合由定时任务周期调用。
- `QuotaCounter.evict_tenant(tenant_id) -> bool`：强制清理指定租户的
  状态，用于**已确认下线**的租户。注意：清理后若该租户再来请求，计数
  会从零开始，因此调用方必须确保其不再活跃。
- `QuotaCounter.tracked_tenants()`：观察当前仍持有状态的租户列表。

边界说明：`reclaim_idle` 只清理严格早于当前窗口的桶，当前窗口内的
活跃计数永远不会被回收；`evict_tenant` 是运维操作，不受此限制。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（身份、配置校验、窗口、并发抢占、后端故障降级、
# 身份拒绝留痕、状态回收、端到端）；默认即打印每个请求的输入与
# 最终判定依据，无需额外参数
uv run pytest

# 启动服务（配置非法时此处直接启动失败）
uv run python main.py   # 监听 127.0.0.1 随机端口

# 手动验证示例
curl -i http://127.0.0.1:<port>/ -H 'X-Tenant-Id: tenant-a'
curl -i http://127.0.0.1:<port>/                      # 401 tenant_missing
```

## 代码结构

- `app/identity.py` — 租户身份解析与校验（缺失/非法/冲突可区分）、审计用租户候选提取
- `app/config.py` — 默认值 + 按租户覆盖的配置，启动时校验
- `app/quota.py` — 固定窗口配额计数器（按租户配置、并发安全、状态回收）
- `app/audit.py` — 按租户隔离的审计记录
- `app/guard.py` — 编排层：配额判定 + 审计 + 任意后端故障按生效策略降级
- `app/errors.py` — 可区分的拒绝原因与错误类型
- `main.py` — 启动时配置校验、FastAPI 中间件挂载与错误响应映射
- `tests/` — 身份、配置、配额、降级、回收、端到端并发测试
