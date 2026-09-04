# agent_runner

链路起点: 剧本 → invoke agent → cleaned-traces → 存 messages trace。
端点: agent/adapter 走 `common.agent_client`(config.yaml `agent.*` 或 env `AGENT_*`)。

## 用法
```bash
# 只产 trace
python -m skills.agent_runner.run --scripts data/results/badcase_scripts.json

# 顺带 golden 判定(需 cached GU)
python -m skills.agent_runner.run --scripts data/results/badcase_scripts.json --with-golden
```

## 前置
内网服务器需可达 edp_agent adapter(默认 edp_agent, 地址在 config.yaml `agent.base_url`)。
不可达则无法产 trace, 只能拿已有 trace 跑后续 golden/coldstart/rules/validate。
