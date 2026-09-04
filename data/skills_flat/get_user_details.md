---
name: get_user_details
description: >
  查询用户资料详情（邮箱、默认地址、支付方式、历史订单等）。需要用户画像、支付方式 id 或帮用户查订单号时使用；前置条件是已认证用户身份。
---

# get_user_details

## 职责
返回指定用户的完整资料，包括邮箱、默认地址、payment_methods（gift card / paypal / credit card 及其余额）以及该用户的订单列表。

## 工具白名单
- `get_user_details`

## 执行流程
认证用户身份（得到 user id）→ 调用 → 使用返回的支付方式 id、订单列表等信息支撑后续查询或写操作。

## 输入槽位
- `user_id`：用户 id，如 `'sara_doe_496'`。

## 强约束
- 必须先认证用户身份，且每次对话只能服务这一个用户，不得查询其他用户的资料。
- 支付方式分为 gift card、paypal、credit card 三类；涉及 gift card 支付/退款时需关注其 balance。
- 不得编造工具未返回的用户信息。
