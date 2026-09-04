---
name: exchange_delivered_order_items
description: >
  对已 delivered 订单中的 item 进行同 product 换 option 的换货。当用户要求换尺码/颜色等时使用；前置条件是已认证用户、订单状态为 delivered、已收集全部待换 item 及目标新 item。
---

# exchange_delivered_order_items

## 职责
将 `delivered` 订单中的若干 item 换成同一 product 下不同 option 的有货新 item，订单状态变为 `exchange requested`，并处理差价。

## 工具白名单
- `exchange_delivered_order_items`

## 执行流程
认证用户 → 用 `get_order_details` 确认订单状态为 `delivered` 并列出可换 item → 用 `get_product_details` 查询目标新 item 是否存在且有货 → 提醒用户确认已提供全部要换的 item → 收集差价收/退款支付方式 → 列出换货明细并获得用户明确确认（yes）→ 一次性调用 → 告知用户状态变更及后续邮件。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，开头有 `#`。
- `item_ids`：待换出的 item id 列表，如 `'1008292230'`，可含重复项。
- `new_item_ids`：换入的新 item id 列表，与 `item_ids` 等长且按位置一一对应，每个新 item 必须是同 product 的不同 option。
- `payment_method_id`：支付或接收差价的支付方式 id，如 `'gift_card_0000000'` 或 `'credit_card_0000000'`，可从用户或订单详情中查得。

## 强约束
- 订单只有状态为 `delivered` 才能换货，调用前必须先检查状态。
- 只能换成同一 product 下不同 option 的有货 item，不能改变 product 类型（如不能把 shirt 换成 shoe）。
- 换货只能调用一次：调用前必须把所有要换的 item 收集成一个列表，并提醒用户确认没有遗漏。
- 用户必须提供支付方式用于支付或接收差价；若为 gift card，余额必须足以覆盖差价。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
- 确认后订单状态变为 `exchange requested`，用户会收到退货指引邮件，无需重新下单。
