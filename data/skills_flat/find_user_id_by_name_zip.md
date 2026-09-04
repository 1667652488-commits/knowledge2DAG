---
name: find_user_id_by_name_zip
description: >
  通过名+姓+邮编查找 user id，是邮箱认证失败时的备选认证方式。仅当用户不知道邮箱或邮箱查不到时使用。
---

# find_user_id_by_name_zip

## 职责
根据用户的名、姓和邮编在数据中定位对应的 user id；未找到时返回错误。

## 工具白名单
- `find_user_id_by_name_zip`

## 执行流程
优先尝试 `find_user_id_by_email` → 邮箱不可用或未找到时，向用户索取名、姓、邮编 → 调用 → 得到 user id 完成认证。

## 输入槽位
- `first_name`：名，如 `'John'`。
- `last_name`：姓，如 `'Doe'`。
- `zip`：邮编，如 `'12345'`。

## 强约束
- 对话开始时必须认证用户身份，即使用户已主动提供 user id 也必须执行认证。
- 默认应先用邮箱认证，本工具仅在邮箱查不到或用户记不得邮箱时使用。
- 认证是提供任何订单、产品、个人资料信息的前置条件；每次对话只能服务一个用户。
