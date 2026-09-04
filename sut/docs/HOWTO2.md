# HOWTO 2 — 剧本数量控制 + agent_adapter 连通验证

> 补充 HOWTO 之外的两个高频操作。

---

## 一、控制 script_gen 生成剧本的数量

`python run.py script_gen` 默认按 config 的 `generation.target_count` 生成(默认 20 条)。两种改法:

### 法 A: CLI 临时指定(不改文件)
```bash
python run.py script_gen --target-count 30
```

### 法 B: 改 config 长期生效
编辑 `skills/badcase_script_gen/config.yaml`:
```yaml
generation:
  target_count: 30          # 改这里
  enable_generalization: true
  output_filename: "badcase_scripts.json"
```

### 优先级
`--target-count` CLI > config `generation.target_count` > 默认 20

### 原理
phase2 的 prompt 里有 `本次需生成 N 条剧本`, N 即 target_count; LLM 按此数量输出 JSON 数组。LLM 不一定严格等于 N(它可能多覆盖弱点), 但会以此为目标, 不会少。

### 跑完看实际数量
```bash
python -c "import json; print(len(json.load(open('data/results/badcase_scripts.json'))))"
```
若多于预期, 手动裁 `badcase_scripts.json`; 若少于, 提高 target_count 重跑。

---

## 二、验证 agent_adapter(edp_agent)是否连通

跑批(`python run.py agent`)前先确认 adapter 通。最简单——调 `skill_list`(一次 POST, 不跑对话):

### 1. 先确认 config 填对
```bash
python -c "import sys; sys.path.insert(0,'.'); from common.config import get_config; a=get_config().agent; print(a.base_url, a.agent_name)"
```
应输出内网 adapter 的 `http://IP:端口` + `edp_agent`。若还是 `localhost:18091`, 改 `config.yaml` 的 `agent.base_url` 或设 env `AGENT_BASE_URL`。

### 2. 调 skill_list 验连通
```bash
python -c "import sys; sys.path.insert(0,'.'); from common.agent_client import skill_list; print(skill_list())"
```

**看输出判断**:
| 输出 | 含义 |
|---|---|
| `{'skills': [...]}` 或技能列表 | **通了** ✓(网络 + agent_name 都对) |
| `{'error': 'HTTP 404'}` 之类 | 网络通, 但路径/agent_name 不对 |
| `{'error': '...Connection refused...'}` 之类 | **不通**(IP/端口/网络/防火墙) |

### 3. 或直接 curl(最原始, 不走 Python)
```bash
curl -X POST 'http://<adapter IP>:<端口>/api/v1/skills' \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"edp_agent","action":"skill_list"}'
```
返回 JSON 技能列表 = 通。

### 4. 通了之后
`skill_list` 通 = adapter 可达 + agent_name 对, 跑批 `python run.py agent --scripts data/results/badcase_scripts.json` 就能产 trace。

### 排错
- `Connection refused`: IP/端口错, 或 adapter 没起, 或防火墙挡。
- `HTTP 404`: base_url 对但 path 不对——确认 adapter 是不是 `/api/v1/skills` 这个路径(本包假设的 edpagent adapter 协议); 若内网 adapter 是另一套 API, `agent_client` 要改。
- `HTTP 401/403`: 鉴权问题(adapter 可能也要 token? 当前 `agent_client` 没带 token, 若需要, 告诉我加)。

---

## 改动记录(本轮)
- `skills/badcase_script_gen/prompts/phase2_script_writing.txt`: 加 `{{target_count}}` 数量要求段
- `skills/badcase_script_gen/run.py`: phase2 传 target_count + 加 `--target-count` CLI + 从 config 读
- 备份: `prompts/phase2_script_writing.txt.bak` + `run.py.bak`(同目录)
