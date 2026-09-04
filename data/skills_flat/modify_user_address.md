---
name: modify_user_address
description: >
  修改用户的默认地址。当用户要求更新其资料中的默认收货地址时使用；前置条件是已认证用户身份。
---

# modify_user_address

## 职责
将指定用户的默认地址整体替换为新地址。

## 工具白名单
- `modify_user_address`

## 执行流程
认证用户身份（得到 user id）→ 向用户索取完整新地址 → 列出新地址明细并获得用户明确确认（yes）→ 调用 → 告知修改结果。

## 输入槽位
- `user_id`：用户 id，如 `'sara_doe_496'`。
- `address1`：地址第一行，如 `'123 Main St'`。
- `address2`：地址第二行，如 `'Apt 1'` 或 `''`。
- `city`：城市，如 `'San Francisco'`。
- `state`：州，如 `'CA'`。
- `country`：国家，如 `'USA'`。
- `zip`：邮编，如 `'12345'`。

## 强约束
- 每次对话只能服务一个已认证用户，只能修改该用户本人的默认地址。
- 这是更新数据库的 consequential action：调用前必须列出操作明细并获得用户明确确认（yes）。
- 本工具改的是用户默认地址，不是某笔订单的收货地址；改订单地址应使用 `modify_pending_order_address`。
