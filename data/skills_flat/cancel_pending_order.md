---
name: cancel_pending_order
description: >
  取消 pending 状态的订单并退款。当用户要求取消尚未处理的订单时使用；前置条件是已认证用户、已用 get_order_details 确认订单状态为 pending。
---

# cancel_pending_order

## 职责
取消一笔状态为 `pending` 的订单，将状态改为 `cancelled`，并按原支付方式退款。

## 工具白名单
- `cancel_pending_order`

## 执行流程
认证用户身份 → 用 `get_order_details` 确认订单存在且状态为 `pending` → 与用户确认 order_id 和取消原因 → 列出取消明细并获得用户明确确认（yes）→ 调用 → 告知用户订单已取消及退款方式/时效。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，注意开头有 `#` 符号。
- `reason`：取消原因，只允许 `'no longer needed'` 或 `'ordered by mistake'` 两个枚举值。

## 强约束
- 订单只有状态为 `pending` 才能取消，调用前必须先检查状态；`processed`/`delivered` 订单不可取消。
- 取消原因必须是 `'no longer needed'` 或 `'ordered by mistake'` 之一，需与用户确认。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
- 退款走原支付方式：gift card 支付立即退回余额，其他支付方式需 5-7 个工作日。
