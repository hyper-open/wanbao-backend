# 积分扣费流程（billing-flow）

> 描述当前线上计费口径与代码链路。计价单位：积分（历史「次」×10 迁移而来，见 points-migration.md）。

## 1. 定价配置（工具侧）

| 字段 | 含义 | 渠道覆盖 |
|---|---|---|
| `tool.price_quota` | 生成/导出消耗积分（confirm 时扣） | ✅ `channel_pricing` 按渠道覆盖 |
| `tool.analyze_quota` | 分析消耗积分（建子任务时扣；0=免费） | ❌ 全局一口价 |

生效定价统一走 `billing.effective_price_quota(tool, channel)`：`channel_pricing[channel]` → 未配置回落 `price_quota`。

## 2. 计费时机（会话式工具，全量两笔）

```
建子任务 ──扣分析费(analyze_quota)──► 分析 → 追问×N（不再扣费）→ 确认卡
                                                            │ 用户点确认
        ◄──────────── 失败/取消/过期：全额退费 ──────────────┤
                                                            ▼
                                              ──扣生成费(price_quota)──► 产出
```

- **分析费**：`session._create_turn` 建子任务即扣（分析 + 全部追问轮共用这一笔）；扣不了（余额不足）则整个子任务不创建
- **生成费**：`session.confirm` 用户点「确认生成」后扣；确认卡价格来自 `_prepare_confirm`（与实际扣费同口径，只读预计算）
- 交互轮数上限 `interaction_config.max_rounds`（默认 5，0=不追问）只影响追问轮数，不影响扣费笔数
- 纯一次性工具（非 interactive）：只有确认后的一笔生成费

## 3. 扣减实现：`billing.consume()`

1. `Account` 行锁（`with_for_update`，防并发双扣）
2. 余额 = `quota_grant` 台账中未过期配额之和（`_live_grants`）
3. 扣减顺序：**优先扣最快到期的**（对用户有利），永久额度兜底
4. `account.quota_used` 累加（统计口径）+ 写 `transaction`（type=consume，ref_id=invocation_id）
5. 余额不足：`sum(remaining) < need` → `ValueError("insufficient_quota")`

## 4. 余额不足的处理

| 入口 | 后端行为 | 小程序表现 |
|---|---|---|
| 建子任务（分析费不足） | 回滚刚建的 turn + invocation（不留脏数据），抛 402 | 弹「积分不足」+「去充值」跳「我的」页 |
| confirm（生成费不足） | 抛 402；子任务保持 `awaiting_confirm`，充值后可直接再确认 | 同上 |

错误码 `insufficient_quota` 由 `utils/request.js` 翻译为「剩余次数不足，请先充值」；`chat.js handleQuota()` 在建会话/追问/确认三个入口统一拦截引导。

## 5. 退费

触发条件（`worker/session_runner.py`）：产出失败、输出层内容安全拒绝、运行卡死超时。

- `billing.refund()`：退还该 invocation **已扣的全部积分**（分析费 + 生成费），回补为一笔**永久** `quota_grant`（不回滚 transaction，另写 type=refund 流水），并修正 `quota_used`
- 幂等防白嫖：confirm 基于子任务行锁 + `turn.charged` 幂等标记，双击/重复请求不会二次扣费

## 6. 数据模型速查

| 表/字段 | 作用 |
|---|---|
| `account.quota_total / quota_used` | 永久额度（充值）与累计消耗（统计口径） |
| `quota_grant.quota / remaining / expires_at` | 每笔配额台账（含有效期），**可用余额的真源** |
| `transaction.type` | recharge / recharge_bonus / consume / refund / checkin_reward / invite_reward |
| `tool_invocation.quota_charged / amount_charged` | 单次调用扣费快照（两笔合计：分析费+生成费） |
| `account.balance` | 历史遗留（元），只进不出，不参与消费 |

## 7. 代码索引

| 环节 | 位置 |
|---|---|
| 生效定价 | `app/services/billing.py::effective_price_quota` |
| 扣费 / 退费 | `app/services/billing.py::consume / refund` |
| 分析费扣减 | `app/api/session.py::_create_turn` |
| 生成费扣减 | `app/api/session.py::confirm` |
| 失败退费 | `app/worker/session_runner.py`（失败处理） |
| 充值/套餐 | `app/api/billing.py`、`app/services/billing.py::mark_order_paid` |
| 前端引导 | `miniprogram/utils/request.js`、`miniprogram/pages/chat/chat.js::handleQuota` |
