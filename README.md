# tenant-quota-guard

多租户请求配额治理层：在 FastAPI 服务前对请求做租户身份校验、按租户的
时间窗口配额计数与超限拦截，并在治理组件异常时按既定策略降级，保证结果
一致、可解释、可审计。

## 租户识别

身份来源（两处同时提供时必须一致）：

| 来源 | 位置 | 说明 |
| --- | --- | --- |
| 请求头 | `X-Tenant-Id` | 首选来源 |
| 查询参数 | `tenant_id` | 兼容来源 |

租户 ID 格式：字母开头，后接字母/数字/连字符/下划线，长度 1~64
（正则 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`）。

身份问题一律拒绝（HTTP 400），原因码可区分：

| reason | 含义 |
| --- | --- |
| `missing_identity` | 两个来源都缺失 |
| `invalid_identity` | 任一来源格式非法（空白、非法字符、超长） |
| `conflicting_identity` | 两个来源均合法但取值不一致 |

## 配额与窗口语义

- **固定窗口**：某租户首次计数时开启窗口，起点为当时时钟读数；窗口长度
  `window_seconds` 到期后，下一次计数自动开启新窗口并将计数重置为 0。
- **超限不计数**：窗口内第 `limit+1` 次及以后的请求被拒绝，且不增加计数；
  响应为 HTTP 429，body 为 `{"error": "quota_exceeded"}`，并携带
  `Retry-After` 响应头（距窗口重置的秒数）。
- **并发安全**：检查窗口与递增计数在同一把锁内完成，同一租户并发突发
  不会放行超过上限的请求（测试中以 300 并发抢 50 配额验证放行数恰好为 50）。
- **租户隔离**：每个租户独立的窗口状态；任一租户超限、重置或组件异常
  均不影响其他租户的计数、审计与判定。
- **可复现**：时钟可注入（`InMemoryQuotaStore(clock=...)`），测试用假时钟
  完整复现窗口推进与重置。

## 异常降级策略

治理组件内部失败（计数后端不可用/写入失败、审计写入失败）时按
`DegradePolicy` 给出明确判定，绝不静默吞异常（异常始终写入日志），
也绝不把堆栈或跨租户信息放进响应：

| 策略 | 判定 | reason | HTTP |
| --- | --- | --- | --- |
| `FAIL_CLOSED`（默认） | 拒绝 | `degraded_fail_closed` | 503 |
| `FAIL_OPEN` | 放行 | `degraded_fail_open`（计数失败）或原 reason + `degraded=True`（审计失败） | 200，带 `X-Quota-Degraded: true` |

每次判定（放行、拒绝、降级）都会写入 `AuditRecord`
（tenant_id / allowed / reason / detail），detail 记录判定依据
（如 `count 3/2 in window, retry after 7.3s`），审计记录按租户隔离，
可通过 `InMemoryAuditSink.for_tenant()` 检索。

## 本地验证

```bash
# 运行全部测试（日志默认打印每次请求的输入与判定依据）
uv run pytest

# 启动服务
uv run python main.py   # 监听 127.0.0.1 随机端口，见启动输出

# 手动验证示例
curl -i http://127.0.0.1:<port>/                                 # 400 missing_identity
curl -i -H 'X-Tenant-Id: acme' http://127.0.0.1:<port>/          # 200
curl -i -H 'X-Tenant-Id: acme' 'http://127.0.0.1:<port>/?tenant_id=other'  # 400 conflicting_identity
```

## 代码结构

```
quota_guard/
  identity.py   # 租户身份解析与校验（缺失/非法/冲突可区分）
  quota.py      # 固定窗口计数器（线程安全、时钟可注入、按租户隔离）
  audit.py      # 审计记录与写入端协议
  guard.py      # 判定编排：身份 -> 计数 -> 审计，含降级策略
  errors.py     # 错误类型与稳定 reason 码
main.py         # FastAPI 接入：Decision -> HTTP 状态码/响应头
tests/          # 身份、配额并发、超限拦截、异常降级、HTTP 层测试
```
