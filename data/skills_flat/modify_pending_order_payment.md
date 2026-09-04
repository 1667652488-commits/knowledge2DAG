---
name: modify_pending_order_payment
description: >
  修改 pending 订单的支付方式。当用户要更换付款方式且订单未处理时使用；前置条件是已认证用户、订单状态为 pending、新支付方式属于该用户且不同于原支付方式。
---

# modify_pending_order_payment

## 职责
将 `pending` 订单的支付方式更换为该用户的另一种支付方式，原支付方式退款、新支付方式扣款，订单保持 `pending`。

## 工具白名单
- `modify_pending_order_payment`

## 执行流程
认证用户 → 用 `get_order_details` 确认订单状态为 `pending` 并查出原支付方式 → 用 `get_user_details` 确认新支付方式存在及（如为 gift card）余额充足 → 列出修改明细并获得用户明确确认（yes）→ 调用 → 告知结果。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，开头有 `#`。
- `payment_method_id`：新支付方式 id，如 `'gift_card_0000000'` 或 `'credit_card_0000000'`，可从用户或订单详情中查得。

## 强约束
- 订单只有状态为 `pending` 才能修改，调用前必须先检查状态。
- 只能更换为单个与原支付方式不同的支付方式。
- 若新支付方式为 gift card，余额必须足以覆盖订单总额。
- 确认后订单保持 `pending`；原支付方式的退款在 gift card 时立即到账，否则需 5-7 个工作日。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
- 对 pending 订单只能修改收货地址、支付方式或 item 选项，其他内容不可修改。（来源：wiki「Modify pending order」总述第 2 条，补译）
