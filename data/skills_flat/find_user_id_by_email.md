---
name: find_user_id_by_email
description: >
  通过邮箱查找 user id，是用户身份认证的首选方式。对话开始需要认证用户身份时优先调用，无其他前置条件。
---

# find_user_id_by_email

## 职责
根据用户邮箱在数据中定位对应的 user id；未找到时返回错误。

## 工具白名单
- `find_user_id_by_email`

## 执行流程
向用户索取邮箱 → 调用 → 得到 user id 完成认证；若返回未找到，改用 `find_user_id_by_name_zip` 通过姓名+邮编认证。

## 输入槽位
- `email`：用户邮箱，如 `'something@example.com'`，匹配不区分大小写。

## 强约束
- 对话开始时必须认证用户身份（通过邮箱或姓名+邮编定位 user id），即使用户已主动提供 user id 也必须执行认证。
- 认证是提供任何订单、产品、个人资料信息的前置条件。
- 每次对话只能服务一个用户，须拒绝与其他用户相关的请求。
- 邮箱查不到用户时，才改用 `find_user_id_by_name_zip`。
