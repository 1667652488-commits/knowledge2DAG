---
name: badcase-script-gen
description: Generate badcase test scripts (剧本) for a skill-based LLM agent, systematically — read SKILL/AgentRule to find weak points, apply software-testing techniques (equivalence/boundary/decision-table/state/negative/error-guessing/use-case) + LLM-agent-specific test types (HITL/routing/orchestration/faithfulness/business-rule/context/boundary), output badcase_scripts.json. Use when the user asks to 构建badcase剧本 / 造测试剧本 / 设计badcase用例 / 写badcase scripts / 找badcase场景 / 生成测试脚本.
---

# Badcase 剧本生成（skill-based LLM agent 测试剧本设计）

为 skill-based LLM agent 系统**系统化构建 badcase 测试剧本**（JSON），用于跑批找 badcase。与 `badcase-curation`（精选）、`golden_data_generator`（独立验 pass_criteria）、`batch_runner`（跑批）组合用，本 skill **单一职责：只造剧本**。

**方法论**：阶段 0 先通读 SKILL/AgentRule 提炼"弱点清单（含相关片段）"→ 阶段 1 覆盖矩阵兜底 → 阶段 2 用测试技术+agent特化类型造剧本 → 阶段 3 输出 JSON。SKILL 全文只读 1 次（成本最低）。

**样例**：`samples/` 下有 5 份既有剧本（kimi_CTX/kimi_nli/mt/new/new2，共 39 条），覆盖 7 种类型，可参考。

---

## 输入读取方法（skill / AgentRule / 场景规则）

phase0 docs = **AgentRule（框架，可选）+ 场景规则 `AgentRule_*.md`（对公主子 dispatch）+ 各 SKILL.md**。三者均从本地读，不联网。路径在 `config.yaml` 的 `paths` 下：

| 配置项 | 默认 | 读法 | 内容 |
|---|---|---|---|
| `skill_root` | `data/skills_cache` | `read_skill_docs`：扁平 `*.md`（stem=skill 名，跳过 `SKILL.md`/`AgentRule.md`），回退子目录 `*/SKILL.md`。**全量读不截断** | 各 skill 的 description/触发词/不要用于/步骤 |
| `agent_rule` | `AgentRule.md`（bundle 根） | 单文件，截断 3000 字；不存在则跳过 | 顶层框架规则（业务范围/planning_steps 通用模板） |
| `scenarios_dir` | `data/skills_cache/scenarios` | `read_scenario_rules`：glob `AgentRule_*.md`，逐个全量读拼成带标注块 | 对公主子场景规则 |

**对公主子场景规则**（`scenarios_dir` 下的 `AgentRule_*.md`，本次重点输入）：
- `AgentRule_task_dispatch.md` — 主 Agent：识别多实体 → `call_multiagent` 并行调度子 Agent → 汇总（用 `corporate_credit_123_multi_skill`）。
- `AgentRule_zhidai.md` — 子 Agent：接收分发任务，`call_multiversatile` 并行工作流（用 `corporate_credit_123_sub_skill`），禁止跨实体。
- `AgentRule_task_dispatch_sub.md` — 主 Agent 全称确认 + 分发流程（`customer_confirm_skill` → `corporate_credit_123_multi_skill`）。

**资产来源 & 刷新**：
- skill：由 `golden_gen --skill-source adapter` 刷到 `data/skills_cache`（经 adapter + jiuwenbox 双 sandbox 校验）。
- 对公场景规则：来自 EDPAgent 部署 assets（`d:/edpagent并行版0729/deployment/assets/zhidaitong_parallel/skills/scenarios/`），已拷贝到 `data/skills_cache/scenarios/`。
- 顶层 AgentRule（框架）：可经 `python -m common.managed_docs agent_rule --out data/skills_cache/AgentRule.md` 从 adapter `/api/v1/managed-docs` 拉（**注意：edp_agent 顶层 agent_rule 是理财域框架，非对公；对公主子规则在场景文件，不在那**）。
- ⚠️ jiuwenbox sandbox（`/tmp/skills`）**只装 SKILL.md，不含场景规则**——场景规则在 EDPAgent 宿主文件系统，不能经 jiuwenbox files API 捞（已实测 `/tmp/skills/scenarios` 不存在）。

---

## 阶段 0：understand（通读 → 弱点清单，SKILL 全文只读 1 次）

读 `--config paths.skill_root`（默认 `data/skills_cache`，扁平 `<name>.md`）下的 SKILL.md（frontmatter description/触发词/不要用于/必填槽位/强约束）+ `agent_rule`（框架，可选）+ `scenarios_dir`（对公主子场景规则 `AgentRule_*.md`）+（若有）golden 全局理解。详见下方「输入读取方法」。

按下面**弱点-finding checklist** 逐维度找弱点，每条弱点**摘相关 SKILL/AgentRule 原文片段**进清单（技术层不再读全文）：

### 弱点-finding checklist（7 维度）

| 维度 | 在 SKILL/AgentRule 找什么 | 弱点样态 |
|---|---|---|
| **HITL** | SKILL「必填+缺失须 ask_user/禁止调用」 | 缺槽位时 agent 是否真调 ask_user 工具（而非 narration 文字） |
| **路由** | 触发词 + 不要用于 + 易混 skill 对 | 触发词未覆盖某表述（如"集团"在公司名里）、两 skill 触发词重叠 |
| **编排（主子 agent 路径规划，本次重点）** | 场景规则 `AgentRule_task_dispatch*` / `AgentRule_zhidai`：主 Agent `call_multiagent` 调度、子 Agent `call_multiversatile` 并行、step1 串行→2/3/4 并行、主子交接 | 漏识别/错识别企业实体、实体数↔子 Agent 数不符、并行汇总丢步/错序、step1 未完成进 2/3/4、baseInfo 未透传、子失败主未兜底；三类场景（单客户单意图/多客户单意图/多客户多意图）逐类找 |
| **忠实** | SKILL「content/attachmentList 原样输出不得改」 | 输出是否忠实源数据（邮件发原文 vs 总结/标题） |
| **业务规则** | 监管规则/业务常识 + skill 是否编码 | 未编码的监管红线（流贷期限/规模额度/报告期间） |
| **上下文** | 多轮指代/附件跨轮 | 第二轮"它"指代、附件跨轮继承是否丢 |
| **越界** | SKILL「不要用于」+ AgentRule §一超范围 | 超 skill 范围请求是否拒答（审计/投资评估等） |

→ 输出 **弱点清单**：`[{skill, 维度, 弱点, 相关SKILL片段, 建议 probe 点}]`。这是技术层的输入。

---

## 阶段 1：覆盖矩阵（兜底，不随机漏）

三维矩阵，每个格子=候选剧本点。弱点清单覆盖的格子直接用；**没覆盖的格子，技术层局部读该 skill 相关片段补造**（不全量重读 SKILL）。

- **skill 维**：每个 skill 至少 1 条（等价类覆盖）。
- **失败维度维**：HITL/路由/编排/忠实/业务规则/上下文/越界，每维至少 1 条。
- **状态维**：槽位（齐/缺/部分/模糊）、多轮（意图变更/取消/指代继承）、目标数（1/N/动态追加）。

> 矩阵防漏，但不求组合爆炸——只覆盖**高风险格子**（弱点清单标的 + 每维每skill至少1）。

---

## 阶段 2：技术层造剧本（测试技术 + agent 特化类型）

拿弱点清单 + 覆盖矩阵，按下面技术造剧本。**经典技术是通用底，LLM-agent 特化类型是聚焦层，叠加用**。

### 经典测试技术 → 剧本类型映射

| 经典技术 | 怎么用 | 产出的剧本类型 |
|---|---|---|
| **等价类划分** | 输入分类：有效单skill/多skill/缺槽/模糊/超范围/违规，每类 1 条 | A 型（单skill单badcase） |
| **边界值** | 槽位刚缺/刚齐、多目标 N 边界（1家vs N家）、上下文 N 轮边界 | 缺槽位、多目标、上下文 |
| **判定表** | 条件组合（skill 模糊 × 槽位 × 业务规则）→ 动作（路由/ask_user/拒答） | 模糊意图 ask_user |
| **状态迁移** | 多轮状态：意图变更/取消/指代继承 | 上下文、取消、意图变更 |
| **负面测试** | 无效/违规输入：超范围、监管违规、越界请求 | 超范围、业务规则、越界 |
| **错误推测** | 经验+SKILL 弱点：触发词不全、未编码规则、易混 | 集团路由、流贷期限 |
| **用例测试** | 端到端真实流：跨skill串行+邮件 | BC13（分层+123+邮件） |
| **组合测试** | 多 skill × 多客户 × 槽位状态 | 多目标批量 |

### LLM-agent 特化测试类型（7 类，聚焦层）

叠在经典技术上，每类至少 1 条：
- **HITL 测试**：缺槽位是否真调 ask_user 工具（不是 narration）。
- **路由测试**：易混 skill 触发词歧义。
- **编排测试**：多目标/多 skill 串行不漏不跳步。
- **忠实测试**：输出忠实源数据（邮件原文 vs 总结）。
- **业务规则测试**：监管/常识违规是否拦截。
- **上下文测试**：多轮指代/附件继承。
- **越界测试**：超 skill 范围请求是否拒答。

---

## 阶段 3：剧本 JSON 模板 + 构建原则

每个剧本按此格式（参考 `samples/`）：

```json
{
  "id": "BCXX",
  "category": "大类",
  "subcategory": "子类",
  "fixed_turns": ["用户第1轮query", "用户第2轮query"],
  "pass_criteria": ["agent 应...（正确行为）", "未..."],
  "expected_badcase": "预期失败模式",
  "fix_target": "（可选,skill-optimizable 时）修哪个 skill/怎么修"
}
```

### 构建原则

- **最小 query**：单句/两轮能稳定触发 badcase，不啰嗦。
- **pass_criteria 写"正确行为"**（agent 应做什么），**不写"假设 agent 会错"**。
- **expected_badcase 写"失败模式"**（预期怎么错）。
- **复现性**：确定性优先（路由/透传/业务规则）；flaky 的（narration/越界）多造几条或广义化提高命中。
- **skill-optimizable 导向**：fix_target 指向修哪个 skill（改 SKILL.md 能解决的），便于后续验证优化效果；排除 AgentRule-required（调用链暴露/重复执行/todolist/多目标编排/取消/超范围——这些改 SKILL 无效）。
- **广义化（G 变体）**：同 badcase 场景换措辞（口语/不同实体），测稳定性 + 提复现率。

### 剧本类型（7 种，按需造）

1. **A 型**（单skill单badcase，等价类+负面）：最轻，单句触发。→ `samples/badcase_scripts_kimi_nli.json`
2. **多目标**（多客户×多skill，边界+组合）：批量/条件分支/动态追加。→ `samples/badcase_scripts_mt.json`（mt_BC20-25）
3. **模糊意图 ask_user**（判定表）：易混 skill 对，模糊意图，应澄清。→ `samples/badcase_scripts_mt.json`（mt_BC26-31）
4. **上下文**（状态迁移）：指代跨skill/附件跨轮。→ `samples/badcase_scripts_kimi_CTX.json`（CTX_BC32-34）
5. **业务规则冲突**（负面+错误推测）：监管/常识违规。→ `samples/badcase_scripts_kimi_CTX.json`（INFO_BC35-37）
6. **广义化 G**（等价+稳健性）：同场景不同措辞。→ `samples/badcase_scripts_kimi_CTX.json`
7. **新造变体**（基于已有 badcase 造变体凑模式）：→ `samples/badcase_scripts_new2.json`

---

## 阶段 4：跑 + 精选 + 验（不包进本 skill，组合工具）

剧本造完后，组合现有工具：
1. **跑批**：`python batch_runner.py --scripts badcase_scripts_xxx.json --output-dir results_xxx` → trace+check+csv。
2. **精选**：用 `badcase-curation` skill（/badcase-curation）从结果精选进 chosen/。
3. **验 pass_criteria**：用 `golden_data_generator`（`test_coldstartGen0626/`）独立生成 expected_behavior，对比剧本 pass_criteria 是否准（golden 不掺项目背景，独立判断）。

完整方法论：`d:/bank_zhidaitong/markdowns/badcase精选与chosen构建方法论.md`。

---

## 工作流（执行顺序）

1. **阶段 0**：通读 SKILL/AgentRule（+ golden GU 若有）→ 按弱点 checklist 输出**弱点清单（含相关片段）**。SKILL 全文只此读 1 次。
2. **阶段 1**：画覆盖矩阵（skill×维度×状态），弱点清单覆盖的标掉，剩高风险格子待补。
3. **阶段 2**：拿弱点清单 + 矩阵，按测试技术+agent特化类型造剧本（漏的格子局部读该 skill 片段补造）。
4. **阶段 3**：输出 `badcase_scripts_xxx.json`（按模板 + 原则）。
5. 交阶段 4（batch_runner/badcase-curation/golden）跑+精选+验。

## 通用性

- 弱点 checklist 7 维度 + 测试技术（经典）+ agent 特化类型（7）均通用，适用任何 skill-based LLM agent。
- 不硬编码具体业务（不写"集团→group"/"流贷期限"），从 SKILL/AgentRule 动态提炼弱点。
- 样例 `samples/` 是智贷通实例，参考结构不照搬内容。
