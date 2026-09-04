# intranet_bundle — 智贷通 agent 测试/规则挖掘内网迁移包

各阶段工具(badcase 生成/精选/golden 生成/cold-start 挖掘/规则生成/验证/跑批)的**内网可运行**打包。
- **messages-only**: trace 只用新 messages 格式, 老 history/agent_result 已废弃
- **common 可配置**: LLM + agent/adapter 对接收进 `common/`, 端点在 `config.yaml`(或 env)
- **golden 当 judge**: 判定靠 golden_gen(产 expected_behavior+result), 不用旧 checker

> ⚠️ "skills/" 只是**文件夹命名**, 不是 Claude Code skill, **不需要任何 skill 运行时**。每个 skill 就是一个普通 Python CLI(`run.py`+argparse), 只依赖 Python 标准库 + `common/` + pyyaml/requests。`SKILL.md` 是方法论文档, 运行时不需要。统一入口 `python run.py <命令>` 从任何目录都能跑。内网只需: Python 3.8-3.10 + 离线装依赖 + 可达 LLM 端点。

## 目录
```
intranet_bundle/
├── config.yaml                 # ★ 单点配置: llm + agent 端点 + paths
├── requirements.txt            # pyyaml, requests
├── common/                     # 共享基础设施
│   ├── config.py               # 读 config.yaml + env 覆盖
│   ├── llm_client.py           # LLM 网关 call_llm()(openai 兼容/mock)
│   ├── agent_client.py         # agent/adapter 网关 invoke/get_cleaned_traces/skill_*
│   ├── trace_io.py             # messages-only trace 读写/解析
│   ├── json_utils.py           # 健壮 extract_json_block + is_llm_error + call_llm_json
│   └── state.py                # runs 持久化(断点续跑/审计)
├── skills/
│   ├── badcase_script_gen/     # 剧本生成(按 skill 切片防上下文爆炸)
│   ├── agent_runner/           # 跑批产 messages trace(invoke+cleaned-traces)
│   ├── golden_gen/             # ★ golden 判定(两阶段: 全局理解→逐条 expected_behavior)
│   ├── coldstart_mining/       # cold-start 规则挖掘(phase2-6, messages-only)
│   ├── rules_gen/              # LLM 生成可执行 Python checker(正例对比+编译期强制)
│   ├── validation/             # 验证准确率(validate) + 静态质量(quality_stats)
│   ├── badcase_curation/       # badcase/goodcase 精选(依赖 golden+cold-start, 不依赖 checker)
│   ├── fault_localization/     # 失败case根因定位→skill/AgentRule(反查 coldstart rule + LLM 兜底)
│   └── trace_md/               # trace→可读 md(调用链摘要+参数解析, 离线)
├── data/                       # 种子/示例(traces/golden/skills_flat/results)
├── docs/HOWTO.md               # ★ 内网实操: 修 group_by_skill bug + 已有剧本跑 golden/全流程
├── docs/HOWTO2.md              # ★ 剧本数量控制 + agent_adapter 连通验证
└── runs/                       # 运行产出(自动)
```

## 部署到内网(离线, 无需 pip 联网)

1. **安装依赖**(wheel 已随包打在 `wheels/` 下, 覆盖 Python 3.8/3.9/3.10 win_amd64):
   - Windows: 双击 `install_offline.bat`(或 `pip install --no-index --find-links wheels/ -r requirements.txt`)
   - 验证: `python -c "import yaml,requests; print(yaml.__version__, requests.__version__)"`
   - 内网若已装 pyyaml/requests 可跳过(常见包, 先查一句)
2. 编辑 `config.yaml`:
   - `llm`: `mode: openai`, `base_url: <内网LLM>/v1`, `model: ...`(`api_key` 留空, 用 env `LLM_API_KEY`)
   - `agent`: `base_url: <内网adapter>:<port>`, `agent_name: edp_agent`(或 env `AGENT_BASE_URL`/`AGENT_NAME`)
3. (可选)mock 冒烟: `llm.mode: mock` + `agent.base_url` 随意, 各 skill 跑通不崩即可

> agent_runner 跑批需内网可达 edp_agent adapter; 不可达则跳过跑批, 拿已有 trace 跑后续。
> 若内网 Python 不是 3.8/3.9/3.10 或非 win_amd64, 需在有网机器 `pip download -r requirements.txt -d wheels/ --platform win_amd64 --python-version <x.y> --only-binary=:all:` 补对应 wheel。

## 全链路(一键顺序)
```bash
# 入口统一用根目录 run.py(从任何目录都能跑, 无需 python -m)
# 1. 产剧本
python run.py script_gen
# 2. 跑批产 trace(+可选 golden)
python run.py agent --scripts data/results/badcase_scripts.json --with-golden
# 3. (若 2 没 --with-golden) 独立跑 golden
python run.py golden --trace-dir data/traces --regenerate-global
# 4. cold-start 挖规则
python run.py coldstart --trace-dir data/traces --golden data/golden/golden_output.jsonl --skills data/skills_flat --output runs/coldstart
# 5. 生成 Python checker
python run.py rules --rules runs/coldstart/merged/final_rules.json --output data/rules_python --traces data/traces
# 6. 验证准确率
python run.py validate --rules-dir data/rules_python --trace-dir data/traces --golden data/golden/golden_output.jsonl
python run.py quality
# 7. 精选 badcase
python run.py curation
# 8. 失败case根因定位 → skill/AgentRule(吃 coldstart 的 final_rules.json)
python run.py localize --golden data/golden/golden_output.jsonl --rules runs/coldstart/merged/final_rules.json --trace-dir data/traces
# 附: trace 转可读 md
python run.py trace_md --trace data/traces/trace_F05.json --output data/md/trace_F05.md
```
`python run.py list` 列出所有命令。也可老式 `python -m skills.xxx.run`(从 bundle 根)。

## 单阶段运行(只跑某一阶段)

`run.py` 从**任何目录**都能跑(自带 bundle 根路径 bootstrap), 不用 `cd` 到包内。每个命令的参数用 `python run.py <命令> --help` 查。下面按"前置/输入/输出/单跑场景"列出, **不必跑全链路**, 按需单跑:

| 命令 | 前置/输入 | 输出 | 典型单跑场景 |
|---|---|---|---|
| `script_gen` | LLM + skills_flat/ + AgentRule.md | `data/results/badcase_scripts.json` | 只想生成/补充剧本 |
| `agent` | **可达 agent adapter** + 剧本 | `runs/agent/<ts>/traces/trace_*.json` (+可选 golden) | 只想跑批产 trace |
| `golden` | LLM + trace 目录 | `data/golden/golden_output.jsonl` + `global_understanding.txt` | 已有 trace, 只想标注/重判 |
| `coldstart` | LLM + trace 目录 + golden + skills_flat | `runs/coldstart/{skill}/...` + `merged/final_rules.json` + 双版本 NL 规则 | 已有 trace+golden, 只想挖规则 |
| `rules` | LLM + `final_rules.json` + trace 目录(作正例/试跑) | `data/rules_python/rules.py` | 已有规则, 只想生成 checker |
| `validate` | (离线) `rules.py` + trace + golden | `validation/validation_report.json` + 准确率 | 只想看 checker 准确率 |
| `quality` | (离线) golden + final_rules | 终端打印质量指标 | 只想看 golden/规则静态质量 |
| `curation` | golden + `final_rules.json` + trace 目录 | `data/chosen/badcase/`+`good/`+`README.md` | 已有 golden+规则, 只想精选 |
| `localize` | golden + `final_rules.json` + trace(兜底用) | `golden_localized.jsonl`+`localization_report.md` | 已有 golden+规则, 定位失败case该改哪个skill |
| `trace_md` | (离线) 单条/目录 trace | `data/md/trace_*.md` | 只想把 trace 转可读报告(PPT/排查) |

### 常见单跑示例

```bash
# 已有 trace, 只跑 golden 全量标注(不跑跑批)
python run.py golden --trace-dir data/traces --regenerate-global --batch-size 10
# 复用已有全局理解, 单条重判
python run.py golden --trace data/traces/trace_F05.json --global-understanding data/golden/global_understanding.txt

# 已有 trace + golden, 只挖规则
python run.py coldstart --trace-dir data/traces --golden data/golden/golden_output.jsonl --skills data/skills_flat --output runs/coldstart

# 已有规则, 只生成 checker + 验准确率
python run.py rules --rules runs/coldstart/merged/final_rules.json --output data/rules_python --traces data/traces
python run.py validate --rules-dir data/rules_python --trace-dir data/traces --golden data/golden/golden_output.jsonl

# 已有 golden + 规则, 只精选 badcase
python run.py curation --golden data/golden/golden_output.jsonl --rules runs/coldstart/merged/final_rules.json --trace-dir data/traces

# 单条 trace 转可读 md(离线, PPT/排查用)
python run.py trace_md --trace data/traces/trace_F05.json --output data/md/trace_F05.md
# 批量转 md + 注入 golden 判定
python run.py trace_md --trace-dir data/traces --output-dir data/md --golden data/golden/golden_output.jsonl

# 只看静态质量(离线)
python run.py quality
```

### 哪些阶段不需要 agent/LLM
- **纯离线**(无 LLM、无 agent): `validate`、`quality`、`trace_md`、`localize`(rule 反查为主, `--no-llm` 时全离线) —— 拿已有 trace/golden/rules 就能跑, 适合先排查/出报告。
- **只要 LLM**(不需 agent): `script_gen`、`golden`、`coldstart`、`rules`、`curation` —— 内网能访问大模型即可, 不必达 agent adapter。
- **必须达 agent adapter**: `agent` —— 跑批产 trace 需要连 edp_agent。不可达就跳过, 拿已有 trace 跑后续。

### 断点续跑 / 状态审计
含 LLM 的阶段(golden/coldstart/rules/curation)中间产物落 `runs/<skill>/<时间戳>/`, 可事后审计; golden 的 `--global-understanding` 缓存可复用(不传 `--regenerate-global` 即复用)。

## 端点配置(env 优先)
| env | 覆盖 |
|---|---|
| LLM_MODE / LLM_BASE_URL / LLM_API_KEY / LLM_MODEL | llm.* |
| AGENT_BASE_URL / AGENT_NAME | agent.* |

明文 api_key **不进包**, 仅 env 入口。

## 架构要点
- **golden 当 judge**(不用 checker): V3 已验证 golden 同时产 expected_behavior+result, 且反 ask_user 伪标注、区分技术限制 vs 流程缺失、忠实性核验, 比旧 checker 严谨。checker.py 已弃。
- **messages-only**: messages 携带 tool_calls(含 ask_user)+reasoning_content(=think)+tool content(=报告)+assistant content(=final)。老 history/agent_result/planning_steps/interrupts 字段全去掉(interrupts 以 ask_user tool_call 表示)。
- **coldstart phase4** 用 `phases/messages_trajectory_config.json` 声明 messages 格式(history_field=messages)。
- **coldstart 双版本 NL 规则**: `final_rules_natural_language.txt`(badcase 评判者风格, 给人读) + `final_rules_diagnostic.txt`(LLM 改写的诊断规则块, 塞入 3 维评估器 `{diagnostic_rules}` 槽位) + `evaluator_prompt_enhanced.md`(诊断块填入 base prompt 后的完整增强评估 prompt, 运行时再填 expected/skill/messages)。同一批 mined rules 两种渲染。LLM 失败/mock 时诊断块降级为机械罗列(不崩)。
- **rules_gen** 正例对比 spec: 喂同 skill 通过 trace 作"必须判通过"正例, 编译期强制, 收紧触发(误报 67%→57%)。

## 已知 gap
- **rules_gen/quality_stats** 等含 LLM 步骤的 skill 在 mock 模式只验证流程不崩, 真实质量需配真实 LLM 跑。
- **badcase_curation** 已改为依赖 golden + cold-start final_rules.json(不依赖 checker); 但若 `final_rules.json` 里某些 badcase trace 的 conversation_id 不在任何 rule 的 `supporting_trajectories` 中, 会落"未归类"桶(走 `_default` 上限)——属预期, 不是 bug。

## 已验证(mock/离线)
- common/: config/llm/trace_io/json_utils/agent_client 全导入+冒烟通过
- golden_gen: 读 messages trace 抽 ask_user tool_call 正确, mock 不崩
- coldstart_mining: 5 phase 导入 OK + common 接入, phase2 输出"第1轮[用户]/第2轮[Agent]/第3轮[工具]"
- rules_gen: 导入 OK(mock 兜底)
- validation: 复现 V3 50% 准确率(23/46, 召回77.8%, 误报56.8%)
- quality_stats: 跑通(golden 100% 字段完整 + 22 规则质量指标)
- badcase_script_gen: mock 端到端不崩, 产出剧本
- badcase_curation: 种子数据(golden53+rules22+traces53)跑通, badcase11+goodcase27+README+对照; 依赖 golden+cold-start, 不依赖 checker
- agent_runner: 导入 OK

## 已去除(过时)
- `checker.py`(旧 judge, V3 不用)、`rules_to_python.py`(旧关键词生成器, 28% 准确率)
- `chat_with_LLM` 明文 api_key、`chat_with_agent` 硬编码 BASE_URL(迁入 common, 可配置)
- `common/tool_runner.py`(死代码且 CLI 错)、`run_scoped_pipeline` 的 messages→history 转换垫片
- 所有 `d:/bank_zhidaitong` 硬编码路径(改相对 bundle 根 + cfg)
