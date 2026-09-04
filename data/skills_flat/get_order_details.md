---
name: get_order_details
description: >
  查询订单状态与详情。需要了解订单状态、item 列表、支付历史时使用；前置条件是已认证用户身份。
---

# get_order_details

## 职责
返回指定订单的完整详情，包括状态、items、支付历史、收货地址等。

## 工具白名单
- `get_order_details`

## 执行流程
认证用户身份 → 调用 → 根据返回的 status（pending/processed/delivered/cancelled）决定后续可执行的操作。

## 输入槽位
- `order_id`：订单号，如 `'#W0000000'`，注意开头有 `#` 符号。

## 强约束
- 必须先认证用户身份，才能提供订单相关信息。
- 订单状态决定可执行动作：只有 `pending` 可取消/修改，只有 `delivered` 可退货/换货；在采取动作前必须先用本工具检查状态。
- 不得编造工具未返回的订单信息。
