# HOWTO — 内网实操手册

本文件解决三个高频问题:
1. 配置内网 LLM 端点(intranet 模式 + .env)
2. 修 `group_by_skill` 的分组 bug(coldstart 挖不出规则的根因)
3. 已有剧本, 直接跑 golden / 全流程(跳过 script_gen)

> 命令均从 bundle 根目录跑(`python run.py ...` 从任何目录都行, 但路径示例按 bundle 根相对写)。

---

## 零、配置内网 LLM(intranet 模式)

内网 LLM 是 AIGC 服务(OpenAI 兼容形): `POST {base_url}`, header `token` + `userId`, body `{messages, stream}`, 返回 `choices[0].message.content`。已内置 `IntranetClient` 支持(messages 原样透传, 无需压扁)。

### 1. 放 token
复制 `.env.example` 为 `.env`, 填真实 JWT token:
```bash
cp .env.example .env
# 编辑 .env:
# LLM_TOKEN=eyJ0eXBl...你的真实token
```
`.env` 自动加载(不进库, 已在 `.gitignore`), 不用每次 `export`。token 也可用 shell env `LLM_TOKEN`(优先级: env > .env > config.yaml)。

### 2. 切 intranet 模式
`.env` 里加一行(或改 `config.yaml` 的 `llm.mode`):
```
LLM_MODE=intranet
```
`config.yaml` 的 `llm.base_url` 已默认填 AIGC URL(`http://aigc.sdc.cs.icbc/mlpmodelservice/aigc/chat/completions`), `user_id=901177633`(header), 不用动。token 是 JWT 会过期, 换了改 `.env` 即可。

### 3. 验证连通
```bash
python -c "
import sys; sys.path.insert(0,'.')
from common.llm_client import call_llm
print(call_llm([{'role':'user','content':'你好'}]))
"
```
应返回 LLM 文本(如模型对"你好"的回答)。若返回 `[LLM ...]` 错误串, 查 token/网络/响应格式。

### 4. 调测(连不通/解析不对时用)
开启后 IntranetClient 会打印每次调用的完整请求(URL / headers(token 脱敏) / body)+ 原始响应行, 方便定位:
```bash
# 法 A: env 开(临时, 不改文件)
LLM_DEBUG=1 python -c "
import sys; sys.path.insert(0,'.')
from common.llm_client import call_llm
print(call_llm([{'role':'user','content':'你好'}]))
"
# 法 B: config.yaml 设 llm.debug: true
# 法 C: 代码里 from common.llm_client import enable_debug; enable_debug()
```
输出形如:
```
[LLM-DBG] attempt 1 POST http://aigc.sdc.cs.icbc/mlpmodelservice/aigc/chat/completions
[LLM-DBG] headers={'Content-Type': 'application/json', 'token': 'eyJ0eXAiVe...g123', 'userId': '901177633'}
[LLM-DBG] body={'messages': [...], 'stream': 'true'}
[LLM-DBG] 响应共 N 行, 原始前 20 行:
[LLM-DBG]   data: {"choices":[{"delta":{"content":"..."}}]}
...
```
把这段贴出来, 我能判断是 token/网络/响应格式哪个问题。

### 注意
- 鉴权是 `token` + `userId` 两个 header(不是 `Authorization: Bearer`, userId 在 header 不在 body)。
- body 是 `{messages, stream:"true"}`(messages 原样透传, system+user 多消息保留), 不带 model; **内网 LLM 流式输出**, IntranetClient 自动解析 SSE(`data:` 逐块累积 `choices[0].delta.content`, `[DONE]` 结束), 单 JSON 也能兜底。
- 若返回不是 `choices[0].message.content` 形, `IntranetClient` 已兜底 `result.finalAnswer`/`result.content`/`content`; 还不对就把响应 JSON 发我, 加解析分支。
- `mode: mock` 仍可流程冒烟(不联网); `mode: openai` 走标准 OpenAI 兼容。

---

## 一、修 group_by_skill 分组 bug

### 现象
跑 `coldstart` 后 `runs/coldstart/merged/final_rules.json` 不出 scoped 规则(只有全局 phase2), curation 的 badcase 全落"未归类"桶。

### 根因
bundle 的 golden 输出**没有 `pattern` 字段**, 而 `group_by_skill` 在 `elif sid in golden` 分支拿到 `skill=None` 后**不 fall through 到 `infer_skill_from_trace`**, 导致所有 trace 被静默丢弃(`groups={} ungrouped=0`)。

### 修法(改一行)

**文件**: `skills/coldstart_mining/run.py`, `group_by_skill` 函数内(约 146-151 行)。

**改前**:
```python
        if sid in custom:
            skill = custom[sid]
        elif sid in golden:
            skill = golden[sid].get("skill")
        else:
            skill = infer_skill_from_trace(t)
```

**改后**(只改 `elif` 那一行):
```python
        if sid in custom:
            skill = custom[sid]
        elif sid in golden and golden[sid].get("skill"):
            skill = golden[sid]["skill"]
        else:
            skill = infer_skill_from_trace(t)
```

含义: golden 里有 skill 才用 golden 的, 否则 fall through 到 `infer_skill_from_trace`(从 trace 的 tool_calls 推 skill)。这样 golden 不带 pattern 也能分组。

### 一行 sed 搞定(bundle 根目录)
```bash
sed -i 's/        elif sid in golden:/        elif sid in golden and golden[sid].get("skill"):/' skills/coldstart_mining/run.py
```

### 改完自检
```bash
python -c "
import sys; sys.path.insert(0,'.')
from skills.coldstart_mining.run import load_golden, load_traces, group_by_skill
g=load_golden('data/golden/golden_output.jsonl'); tr=load_traces('data/traces')
groups,_=group_by_skill(tr,g,{},min_traces=2)
print('groups:',{k:len(v) for k,v in groups.items()})
"
```
预期输出非空(类似 `{'customer_tiering': 12, 'industry_classification': 4, ...}`)。若仍为 `{}`, 检查 sed 是否改到、缩进是否对(必须是 8 空格缩进)。

---

## 二、已有剧本, 直接跑 golden / 全流程

### 剧本格式(agent_runner 认的)

一个 JSON 文件, **list**, 每条至少 `id` + `fixed_turns`:
```json
[
  {"id": "R01", "category": "路由-集团", "skill": "group_credit_risk", "fixed_turns": ["分析恒大集团的信用风险"]},
  {"id": "F05", "category": "数据透传-baseInfo", "skill": "corporate_credit_123", "fixed_turns": ["先对小米科技做客户分层分类，然后做公司信贷123金融服务方案"]}
]
```
- `id`: 剧本唯一标识(产出的 trace 叫 `trace_<id>.json`)
- `fixed_turns`: 用户每一轮输入文本(按顺序逐轮 invoke agent)
- `category`/`skill`: **可空**。空也能跑——golden 会用 LLM 给 scenario 打标签, coldstart 用 `infer_skill_from_trace` 从 tool_calls 推 skill。但 `category` 填上能让 curation 的 goodcase 分桶更稳(不靠 LLM 标签)。

### 场景 A: 只想跑 golden(已有 trace, 不跑跑批)

适合: 已有 trace 目录, 只想标注 expected_behavior + result。

```bash
# 全量标注(首次会先跑全局理解 phase1, 再逐条 phase2)
python run.py golden --trace-dir data/traces --regenerate-global --batch-size 10

# 复用已有全局理解, 单条重判
python run.py golden --trace data/traces/trace_F05.json --global-understanding data/golden/global_understanding.txt
```
产出: `data/golden/golden_output.jsonl` + `data/golden/global_understanding.txt`

### 场景 B: 已有剧本, 跑全流程(跳过 script_gen)

```bash
# 0. 你的剧本路径(示例)
SCRIPTS=/path/to/你的剧本.json

# 1. 跑批产 trace(需内网可达 agent adapter)
python run.py agent --scripts $SCRIPTS
#   产出: runs/agent/<时间戳>/traces/trace_<id>.json + batch_result.csv
#   只跑部分 id:  python run.py agent --scripts $SCRIPTS --ids R01,F05

# 把产出的 trace 目录记下来(下面用 $TD 代指)
TD=runs/agent/20260701_120000/traces

# 2. golden 全量判(产 expected_behavior+result+scenario)
python run.py golden --trace-dir $TD --regenerate-global

# 3. cold-start 挖规则(需先修上面的 group_by_skill bug!)
python run.py coldstart --trace-dir $TD --golden data/golden/golden_output.jsonl --skills data/skills_flat --output runs/coldstart

# 4. 生成 Python checker + 验准确率
python run.py rules --rules runs/coldstart/merged/final_rules.json --output data/rules_python --traces $TD
python run.py validate --rules-dir data/rules_python --trace-dir $TD --golden data/golden/golden_output.jsonl

# 5. 精选 badcase/goodcase
python run.py curation --golden data/golden/golden_output.jsonl --rules runs/coldstart/merged/final_rules.json --trace-dir $TD
```

### 跑批 + golden 一把梭(agent 内联 golden)
若已有 `global_understanding.txt`, 跑批时可顺带逐条判 golden:
```bash
python run.py agent --scripts $SCRIPTS --with-golden --gu-path data/golden/global_understanding.txt
# 产出: runs/agent/<ts>/traces/ + runs/agent/<ts>/golden/golden_output.jsonl
```
之后跳过场景 B 的第 2 步, 直接从 coldstart 接着跑。

### 注意
- **`agent` 必须可达 agent adapter**(`config.yaml` 的 `agent.base_url`, 默认 edp_agent)。内网连不上就跑不了批——只能拿已有 trace 从 `golden` 起跑(场景 A)。
- **`--with-golden` 要先有 `global_understanding.txt`**: 第一次跑建议先单独跑场景 A 的全量 golden 生成 GU, 之后才能 `--with-golden` 复用。
- **coldstart 分组要 ≥2 条同 skill**: `--min-traces`(默认 2)。若你的剧本每 skill 只 1 条, scoped 不挖, 加 `--min-traces 1` 或多放几条同 skill 剧本。
- 产出的 trace 是 **messages 格式**, 后续所有阶段都认。

### 不需要 agent / 不需要 LLM 的阶段(按需单跑)
- 纯离线(无 LLM 无 agent): `validate`、`quality`、`trace_md`
- 只要 LLM(不需 agent): `golden`、`coldstart`、`rules`、`curation`、`script_gen`
- 必须达 agent: `agent`

```bash
# 例: 只把单条 trace 转可读 md(PPT/排查)
python run.py trace_md --trace data/traces/trace_F05.json --output data/md/trace_F05.md
```
