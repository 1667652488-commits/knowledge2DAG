# trace_md

trace → 可读 markdown 报告。messages-only, 离线(无 LLM/agent 依赖)。

## 用法
```bash
# 单条
python -m skills.trace_md.run --trace data/traces/trace_F05.json --output data/md/trace_F05.md
# 批量(注入 golden 判定)
python -m skills.trace_md.run --trace-dir data/traces --output-dir data/md --golden data/golden/golden_output.jsonl
```

## 输出结构
- 概览: script_id / conversation_id / messages 数 / errors / category / skill / golden 判定(可选)
- **调用链摘要**: 编号列出所有工具调用 + query_intent/script/attachmentList 长度, 一眼看清链路
- 逐 message: 角色 + content + tool_call 解析(参数双编码解开) + reasoning 折叠 + tool 返回状态(✅success/❌sandbox/ℹ对象引用)

## 价值
- 多技能长链路(如 F05: 10 调用/5 skill)一键可视化, 看 PPT/排查都用
- attachmentList 解析失败时 regex 兜底抽长度, 能发现"重试截断"这类埋在深处的难定位问题
