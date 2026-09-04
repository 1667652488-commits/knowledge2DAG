---
name: modify_pending_order_address
description: >
  修改 pending 订单的收货地址。当用户要求改收货地址且订单尚未处理时使用；前置条件是已认证用户、已确认订单状态为 pending。
---

# modify_pending_order_address

## 职责
将 `pending` 订单的收货地址整体替换为新地址。

## 工具白名单
- `modify_pending_order_address`

## 执行流程
认证用户 → 用 `get_order_details` 确认订单状态为 `pending` → 向用户索取完整新地址 → 列出新地址明细并获得用户明确确认（yes）→ 调用 → 告知修改结果。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，开头有 `#`。
- `address1`：地址第一行，如 `'123 Main St'`。
- `address2`：地址第二行，如 `'Apt 1'` 或 `''`。
- `city`：城市，如 `'San Francisco'`。
- `state`：州，如 `'CA'`。
- `country`：国家，如 `'USA'`。
- `zip`：邮编，如 `'12345'`。

## 强约束
- 订单只有状态为 `pending` 才能修改，调用前必须先检查状态。
- 对 pending 订单只能修改收货地址、支付方式或 item 选项，其他内容不可修改。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
