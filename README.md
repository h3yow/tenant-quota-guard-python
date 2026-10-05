# tenant-quota-guard-python

为现有 FastAPI 服务提供多租户请求配额治理层：租户身份校验、按租户配置的固定窗口
配额计数、超限拦截、治理后端任意故障下的显式降级、按租户隔离的审计记录，
以及历史租户状态的可控回收。

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

**身份校验未通过的拒绝同样写入审计**：归属键取请求中给出的原始租户值
（缺失时为 `<unknown>`），与正常判定一样可追溯，且各归属键之间严格隔离。

## 按租户配置与默认值

配置集中在 `QuotaSettings`（`app/config.py`），服务启动时一次性加载并校验，
非法配置（非正上限/窗口、非法租户 ID、未知策略）直接抛 `ValueError`，
服务启动即失败，不会等请求打进来才暴露。

- 默认值：`default_limit` / `default_window_seconds` / `default_policy`，
  适用于所有没有单独配置的租户。
- 按租户覆盖：`tenant_overrides`，可为指定租户单独配置
  `limit` / `window_seconds` / `policy`；未覆盖的项沿用默认值
  （例如只覆盖上限的租户仍使用默认窗口与默认降级策略）。

通过环境变量配置（见 `main.py` 的 `load_settings`）：

```bash
QUOTA_DEFAULT_LIMIT=100            # 默认上限，默认 100
QUOTA_DEFAULT_WINDOW=60            # 默认窗口秒数，默认 60
QUOTA_DEFAULT_POLICY=fail_closed   # 默认降级策略：fail_closed / fail_open
QUOTA_TENANT_OVERRIDES='{"tenant-vip": {"limit": 1000, "window_seconds": 10, "policy": "fail_open"}}'
```

## 配额与窗口语义

- 固定窗口计数：每个租户按**自己生效的** `window_seconds` 对齐窗口，
  即 `window_start = floor(now / window_seconds) * window_seconds`。
- 进入新窗口时该租户的计数自动重置，语义确定、可复现（测试中以注入的 `now` 验证）。
- 计数按租户分桶、互不影响；同一租户在自己的窗口内并发突发通过锁保证不超放，
  不同租户的上限、窗口、计数与判定结果互不影响。
- 超限响应携带 `Retry-After`（距该租户下一窗口的秒数）。

## 后端故障与降级行为

计数后端在真机上会以各种方式失败——连接失败（`ConnectionError`）、
响应超时（`TimeoutError`）、未预料的运行错误（`RuntimeError`）等。
治理层对**所有**异常类型统一处理：按该租户生效的 `DegradationPolicy` 降级，
判定结果、审计记录、对外响应三者语义一致。

- `FAIL_CLOSED`（默认）：拒绝，返回 503 `quota_unavailable`，
  携带 `Retry-After`（该租户生效窗口长度）作为重试提示；
  审计记录 `decision=rejected, reason=quota_unavailable`。
- `FAIL_OPEN`：放行并标记 `degraded`（HTTP 层以 `X-Quota-Degraded: true` 体现）；
  审计记录 `decision=allowed, reason=degraded_fail_open`。
- 审计写入失败：不影响本次判定结果，但判定标记 `degraded=True` 并记录错误日志，
  绝不静默吞异常。
- 未知内部异常：兜底转为 503 `quota_unavailable`，不向外暴露堆栈或跨租户信息。

## 状态回收

长期运行后历史租户的计数状态不能无限堆积，`QuotaCounter` 提供两种可控回收方式
（`QuotaGuard` 上有同名委托方法）：

- `reclaim_expired(now)`：**安全回收**。只清理"窗口已经完整过去"的租户桶——
  这些计数对当前及未来判定本就无效（下次判定必然重置），因此与并发请求判定
  同时进行结果也一致，不会在窗口边界附近漏计或误判。按各租户自己的窗口长度
  分别判断是否过期，不影响其他租户。适合定期调用。
- `reclaim_tenants(tenant_ids)`：**显式回收**。连当前窗口的计数一起移除，
  只用于已确认下线、不再活跃的租户。
- `tracked_tenants()`：查看当前仍持有计数状态的租户，用于观测回收效果。

两种回收与 `try_acquire` 共用同一把锁，清理与判定之间不会交错出不一致状态。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（身份、按租户配置、窗口、并发抢占、后端故障降级、状态回收、端到端）
# 默认即展示每个请求的输入与最终判定依据（pyproject.toml 中 addopts = "-s"）
uv run pytest -q

# 启动服务（非法配置会在启动时直接报错退出）
uv run python main.py   # 监听 127.0.0.1 随机端口

# 手动验证示例
curl -i http://127.0.0.1:<port>/ -H 'X-Tenant-Id: tenant-a'
curl -i http://127.0.0.1:<port>/                      # 401 tenant_missing

# 带按租户覆盖启动
QUOTA_TENANT_OVERRIDES='{"tenant-vip": {"limit": 2, "window_seconds": 10, "policy": "fail_open"}}' \
  uv run python main.py
```

## 代码结构

- `app/config.py` — 默认值 + 按租户覆盖的配置与启动校验、降级策略枚举
- `app/identity.py` — 租户身份解析与校验（缺失/非法/冲突可区分）
- `app/quota.py` — 固定窗口配额计数器（按租户窗口、并发安全、状态回收）
- `app/audit.py` — 按租户隔离的审计记录
- `app/guard.py` — 编排层：身份校验 + 配额判定 + 审计 + 按租户降级策略
- `app/errors.py` — 可区分的拒绝原因与错误类型
- `main.py` — 配置加载、FastAPI 中间件挂载与错误响应映射
- `tests/` — 身份、配置、配额、降级、回收、端到端并发测试
