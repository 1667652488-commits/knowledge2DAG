# mock_versatile_llm 在 mail_send 场景下不参与

> 结论：mail_send_skill 走 `call_mcp` 发 HTTP，**不调 call_versatile**；
> 而 mock_versatile_llm 只接 call_versatile（经 versatile_adapter）那条路。
> 所以 **mail_send 场景下 mock_versatile_llm 完全不被调用，不会有任何反馈**。

## 证据

### 1. mock_versatile_llm.py 没有任何 mail 相关路由

`grep -niE "mail|sendmag|call_mcp|mcp|/icbc" mock_versatile_llm.py` → 全空。

mock_versatile_llm.py 只注册了 3 条路由：
- `/health`
- `/v1/chat/{conversation_id}`（EDPA 指南的 chat 路由）
- `/v1/{tenant}/agent-manager/workflows/{workflow_id}/conversations/{conversation_id}`（adapter 调 mock 的实际路由）

全是 call_versatile 的路。

### 2. mail_send_skill SKILL.md 明确不调 call_versatile

原文：
> 邮件发送智能体 … 通过调用 `/icbc/digitalhuman/app/mail/sendMagMail` 接口实现邮件发送功能，
> **不调用 call_versatile**。
> … 本 Skill 不调用 `call_versatile`，改用 `call_mcp` 调用脚本执行 HTTP POST 请求。

第三步"发送邮件（call_mcp）"：`call_mcp(...)` 执行脚本 → HTTP POST `/icbc/digitalhuman/app/mail/sendMagMail`。

## 两条路是分开的

```
call_versatile 路：  agent → versatile_adapter → mock_versatile_llm     ✅ mock 管
call_mcp 路：       agent → call_mcp → 执行脚本 HTTP POST → /icbc/.../sendMagMail   ❌ mock 不管
```

mail_send 走第二条，mock_versatile_llm 在第一条上，**两者不交汇**。

## 影响

1. **mail_send 的实际发送在 mock 环境里没被 mock**：call_mcp 会去打真实的
   `/icbc/digitalhuman/app/mail/sendMagMail`，mock 环境连不通 → 这一步会失败/超时/报错。
2. **F02/BC13 那条 trace 没碰到这个问题**：agent 卡在 corporate_credit_123 的 14 次
   call_versatile 循环里，根本没走到 mail_send 那步就 ask_user 摆烂了。
   所以 mail_send 的 mock 缺口在 F02 里没暴露。
3. **要测 mail_send 链路**（报告→发邮件），得单独给 `/icbc/.../sendMagMail` 做个 mock
   （或在 mock_versatile_llm 之外另起一个 call_mcp 的 mock 服务），否则 send 步骤必败。

## 补充：mail_send 还有个 ask_user 前置

mail_send 要求 `mailTo`（收件人）缺失时必须 `ask_user` 问用户。
所以即使 send 接口 mock 了，agent 也可能先 ask_user 中断
（这在 trace 里是 ask_user tool_call，不是 call_versatile/call_mcp）。

## 相关

- skill 分类与依赖：[skills_overview.md](skills_overview.md)
- F02 过度推理 badcase（call_versatile 循环，未到 mail_send）：记忆 `f02-over-reasoning-badcase`
- mock 契约：记忆 `mock-versatile-business-data-contract` / `mock-versatile-adapter-path-contract`
