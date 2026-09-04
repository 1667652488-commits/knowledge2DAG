---
name: list_all_product_types
description: >
  列出商店全部 50 种 product type 的名称与 product id。当用户不知道 product id、需要按名称定位产品时使用；前置条件是已认证用户身份。
---

# list_all_product_types

## 职责
返回全部 product type 的名称到 product id 的映射（按名称排序）；每个 product type 下有若干不同 option 的 item。

## 工具白名单
- `list_all_product_types`

## 执行流程
认证用户身份 → 用户描述产品名称但不知 product id 时调用 → 从映射中取得 product id → 再调 `get_product_details` 查具体 variant item。

## 输入槽位
- 无参数。

## 强约束
- 商店只有 50 种 product type，超出列表的产品不存在，不得编造。
- 本工具只给 product id；具体 item 的 option、价格、有货状态需再用 `get_product_details` 查询。
