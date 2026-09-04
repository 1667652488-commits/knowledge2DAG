---
name: return_delivered_order_items
description: >
  对已 delivered 订单中的部分或全部 item 发起退货。当用户要求退货且订单已送达时使用；前置条件是已认证用户、订单状态为 delivered、已确定退货 item 列表和退款支付方式。
---

# return_delivered_order_items

## 职责
将 `delivered` 订单中的若干 item 发起退货，订单状态变为 `return requested`，退款打向原支付方式或一张已有 gift card。

## 工具白名单
- `return_delivered_order_items`

## 执行流程
认证用户 → 用 `get_order_details` 确认订单状态为 `delivered` 并列出可退 item → 与用户确认 order_id、退货 item 列表、退款支付方式 → 列出退货明细并获得用户明确确认（yes）→ 调用 → 告知用户状态变更及后续退货指引邮件。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，开头有 `#`。
- `item_ids`：待退货的 item id 列表，如 `'1008292230'`，可含重复项。
- `payment_method_id`：接收退款的支付方式 id，必须是原支付方式或一张已有 gift card，如 `'gift_card_0000000'` 或 `'credit_card_0000000'`。

## 强约束
- 订单只有状态为 `delivered` 才能退货，调用前必须先检查状态。
- 退款只能打回原支付方式，或一张已有 gift card，不接受其他支付方式。
- 这是更新数据库的 consequential action：调用前必须列出操作明细（order id、退货 item 列表、退款支付方式）并获得用户明确确认（yes）。
- 确认后订单状态变为 `return requested`，用户会收到如何退回商品的邮件。
