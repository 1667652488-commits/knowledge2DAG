---
name: get_product_details
description: >
  查询某个 product 的库存详情及各 variant（item）的选项、价格与有货状态。换货/改 item 前确认目标 item 可用性时使用；前置条件是已认证用户身份。
---

# get_product_details

## 职责
返回指定 product 的详情，包括其全部 variant item 的 id、options、价格和 available 状态。

## 工具白名单
- `get_product_details`

## 执行流程
认证用户身份 → （如不知道 product id，先调 `list_all_product_types`）→ 调用 → 从 variants 中找到满足用户需求且 available 的 item id。

## 输入槽位
- `product_id`：产品 id，如 `'6086499569'`。注意 product id 与 item id 不同，二者无关联，不能混淆。

## 强约束
- product id 与 item id 是两套独立 id，不可混用：传入 item id 会返回 product not found。
- 换货/修改 item 时，目标新 item 必须在同一 product 的 variants 内且 available 为真。
- 不得编造工具未返回的产品或库存信息。
