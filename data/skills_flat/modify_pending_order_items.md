---
name: modify_pending_order_items
description: >
  修改 pending 订单中的 item 为同 product 不同 option 的新 item（整个订单仅能调一次）。当用户要换尺码/颜色等且订单未处理时使用；前置条件是已认证用户、订单状态为 pending、已收集全部待改 item。
---

# modify_pending_order_items

## 职责
将 `pending` 订单中的若干 item 改为同一 product 下不同 option 的有货新 item，订单状态变为 `pending (item modified)`，并用指定支付方式结算差价。

## 工具白名单
- `modify_pending_order_items`

## 执行流程
认证用户 → 用 `get_order_details` 确认订单状态为 `pending` 并列出可改 item → 用 `get_product_details` 确认目标新 item 存在且有货 → 提醒用户确认已提供全部要改的 item → 确定差价收/退款支付方式 → 列出修改明细并获得用户明确确认（yes）→ 一次性调用 → 告知结果。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，开头有 `#`。
- `item_ids`：待修改的 item id 列表，如 `'1008292230'`，可含重复项。
- `new_item_ids`：修改后的新 item id 列表，与 `item_ids` 等长且按位置一一对应，每个新 item 必须是同 product 的不同 option。
- `payment_method_id`：支付或接收差价的支付方式 id，如 `'gift_card_0000000'` 或 `'credit_card_0000000'`。

## 强约束
- 订单只有状态为 `pending` 才能修改，调用前必须先检查状态。
- 本操作只能调用一次，且会把订单状态改为 `pending (item modified)`，此后该订单不能再被修改或取消——调用前务必把所有要改的 item 收集到一个列表中，并提醒用户确认没有遗漏。
- 只能改为同一 product 下不同 option 的有货 item，不能改变 product 类型（如不能把 shirt 改成 shoe）。
- 用户必须提供支付方式支付或接收差价；若为 gift card，余额必须足以覆盖差价。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
- 对 pending 订单只能修改收货地址、支付方式或 item 选项，其他内容不可修改。（来源：wiki「Modify pending order」总述第 2 条，补译）
