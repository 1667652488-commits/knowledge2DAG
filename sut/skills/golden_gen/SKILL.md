# golden_gen

从直觉生成 golden 数据: 两阶段(全局理解 → 逐条 expected_behavior)。
judge = LLM, 产 expected_behavior + result + reason。messages-only。

对齐 evo_agent 栈 `evaluator/golden_data` 的特性(见记忆 [[golden-gen-progressive-exposure]] /
[[zdt-adapter-8900-contract]]): phase2 prompt 两段新增、index 结构化 frontmatter、run workspace、
flat_threshold 自动模式、skill 取数(adapter 权威 → jiuwenbox 双 sandbox 校验回退 → 本地缓存, 不一致中断)。**未对齐**的项(刻意保留 golden_gen 现状): JSONL 全字段输出
(不裁 to_external)、LLM 失败降级(不抛错)、链式多归属路由(不简化为单 skill)。

## 用法
```bash
# 批量(默认 auto 模式: skill 数 > --flat-threshold 走 progressive, 否则 flat)
python -m skills.golden_gen.run --trace-dir data/traces --output data/golden/golden_output.jsonl --regenerate-global --batch-size 10

# 强制 flat / 强制 progressive
python -m skills.golden_gen.run --flat    --trace-dir data/traces --regenerate-global
python -m skills.golden_gen.run --grouped --trace-dir data/traces --skill-dir data/skills_flat --regenerate-global

# skill 经 adapter(权威): 运行时调 8900 skill_list/skill_content, 取回缓存到 --skill-cache-dir, 失败回退缓存
python -m skills.golden_gen.run --skill-source adapter --grouped --trace-dir data/traces --regenerate-global

# 单条(复用全局理解)
python -m skills.golden_gen.run --trace data/traces/trace_B01.json
```

## 模式判定
- `--grouped` 强制 progressive; `--flat` 强制 flat; 都不指定 = **auto**(`skill 数 > --flat-threshold`→progressive, 否则 flat, 默认阈值 30)。
- 向后兼容: `--global-understanding` 指向已建分组目录(含 `index.md`)时自动走 progressive。

## 输出
JSONL 每行(全字段, 不裁对外口径): `{id, script_id, inputs, expected_behavior, result, reason, scenario, score?}`
- expected_behavior 三段式: 在xxx情况下，应该xxxx，不应该xxxx
- result: 通过 / 部分通过 / 失败 / NA(按 phase2「result 判定标准」: substance vs manner, 外部中断/交互方式不降档)

## 关键设计
- **粒度与抽象化原则**(phase2 新增): EB 只用业务语言, 不得出现 trace 代码标识符(函数名/字段名/状态码/步骤透传/枚举/具体取值)——否则 rollout 优化信号会被误导到实现层。给了对/错示范。
- **result 判定标准**(phase2 新增): 通过/失败/部分通过按性质判别, 区分核心达成(substance) vs 交互方式(manner); 不降档情形(仅交互方式可优化、外部中断/未闭环 → 不算失败)。
- **反 ask_user 伪标注**: content 出现追问措辞但 trace 无 tool_calls/中断证据 → 判失败
- **技术限制 vs 流程缺失**区分: 工具失败/超时/MCP 不通时 expected_behavior 须含兜底话术
- **技能选择两步推理**: 先按语义意图独立判技能, 再对比 agent 实际调用(不把 agent 调用当基线)
- **忠实性核验**: 发邮件/导出须忠实原文, 总结/改写/精简判失败
- **链式多归属路由**(progressive): 一条 trace 涉多 skill 时同时归多组, 用时灌多份 sub
- 不注入 reasoning_content(非可见行为, 防上下文爆炸)

## GU 持久化结构(progressive 模式)
`global_understanding/` 目录:
- `index.md` — **YAML frontmatter**(`mode/skills/last_run_id/out_of_scope_count`, 程序化可读, `load_index()` 解析) + 链式 trace 表 + OOS 计数正文
- `system_wide.md` — 跨 skill 涌现共性
- `per_skill/<skill>.md` × N — 各 skill 局部 sub; `per_skill/__out_of_scope__.md` — 越界伪组

## run workspace(中间结果, 可追溯)
每次建 GU 生成 `run_id`, 中间结果落 `<GU 目录旁>/.gu_runs/gu_<run_id>/`(或 `--intermediate-dir`):
- `mode_decision.json` — mode/skill_count/flat_threshold/skill_names/trace_count/batch_size
- `trace_skill_routes.json`(progressive) — {skill: trace 数}
- `per_skill_draft/<skill>/`(progressive) / `global_batch_N.txt`(flat) — 批 induct/refine 草稿
- `index.md` 的 `last_run_id` frontmatter 字段可回溯到对应 run workspace。

## skill 取数(两源: cache 默认 / adapter 刷新)
- `--skill-source cache`(**默认, 零网络, 日常用**): 直接读 `--skill-cache-dir`(默认 `data/skills_cache`)扁平 `<name>.md`。空则降级无 skill 跑(提示先刷一次)。
- `--skill-source adapter`(**刷新, 经 adapter**): 三级——① 经 8900 `skill_list`/`skill_content`(配 `--agent-name`, 默认 edp_agent)取 skill; 取到非空 → 刷 `--skill-cache-dir`(先清旧 `*.md`) → 返回。② adapter 失败(multiple-sandboxes/HTTP 500, edp_agent 现状)→ 回退 jiuwenbox(8321)读**每个** ready sandbox 全量 skill(`/api/v1/sandboxes` + `/files` + `/download`)逐字节比对: 名集同 + 每个 skill 内容同 → 通过刷缓存+返回; **不一致 → `SystemExit` 中断**(环境漂移, 不跑 golden_gen, 不降级); jiuwenbox 不可达 → 继续 ③。③ 回退读 `--skill-cache-dir` 上次扁平 .md(warn), 无则 `{}`。

**日常模型**: 平时跑 `cache`(零网络, 快); 要更新 skill 时显式跑一次 `--skill-source adapter` 刷 `data/skills_cache`。
- 两个 sandbox 是并行执行设计(主/子 agent 各一), skill 理论一致; adapter 刷新走 jiuwenbox 时硬校验, 漂移即中断。
- **cache 是策展子集**: `data/skills_cache` 只放当前项目要用的 skill(如只 4 个对公); adapter 上是全量(如 12 个)。**刷新 adapter 会清旧写全量, 把删过的又拉回来**(不加过滤, 靠人工策展; 不同项目 skill 掺一起是特殊情况)。
- jiuwenbox_url 见 `config.yaml` `agent.jiuwenbox_url`(env `JIUWENBOX_URL` 优先)。
- 注: 旧的 `--skill-source local`(子目录 `*/SKILL.md`)已删, `data/skills_flat` 双重布局废弃。

## 端点
LLM 走 `common.llm_client`, 端点见 `config.yaml`(`llm.*`)或 env(`LLM_*`)。
adapter 端点见 `config.yaml`(`agent.base_url/agent_name`)或 env(`AGENT_BASE_URL/AGENT_NAME`)。
