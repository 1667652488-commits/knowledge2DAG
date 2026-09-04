# Demo 演示界面设计思路

> 用 bundle 现有 mock 产出（不重跑），搭一个**端到端演示界面**：展示"冷数据 → 黄金数据集 → 评估器加强提示词"等关键节点的输入输出，并在每个自动节点后插**人工审阅闸**（审阅-筛选-编辑规则/expected_behavior 等）。
> 本质：给自动 pipeline 套一层 human-in-the-loop 资产打磨层。

---

## 已对齐的三个方向

| 维度 | 选定 |
|---|---|
| 形态 | **静态 HTML（无依赖）** |
| 节点范围 | **全 6 节点 + 审阅** |
| 审阅定位 | **演示型**（存 localStorage + 导出 JSON，不求多用户持久化） |

---

## 技术形态（关键约束）

纯静态 HTML 双击打开时，浏览器 `file://` 协议**禁止 fetch 本地 JSON**（CORS）。
→ 解法：写 Python 脚本 `build_demo.py` 读 bundle 产出 → 把数据 **inline 嵌进一个自包含 `demo.html`**（数据 + UI + JS 全在一个文件）。
→ 双击即开、无依赖、无 CORS、可编辑可导出。

**构建链**：`build_demo.py`（读 JSON/txt）→ 生成 `demo.html`（自包含演示页）。

---

## 界面布局

```
┌─ 顶栏：标题 + [导出 curated JSON] [重置] ─────────────────────┐
├─ 左导航(6节点, 带状态徽章 待审N/已审M) ─┬─ 右主面板(当前节点) ──┤
│  ① 冷数据输入            ▸              │  数据列表 + 每条记录卡  │
│  ② 黄金数据集生成  待12已5              │  + 审阅操作(接受/拒绝/编辑)│
│  ③ 规则挖掘        待18已4              │                        │
│  ④ 评估器加强提示词  待22               │                        │
│  ⑤ 定位            待9已4               │                        │
│  ⑥ 精选回归        待11                 │                        │
└─────────────────────────────────────────┴────────────────────────┘
```

---

## 各节点记录卡 + 审阅操作

| 节点 | 记录卡字段 | 审阅操作 |
|---|---|---|
| ① 冷数据 | trace: script_id / 轮次摘要 / tool_calls | 只读浏览（展示输入长啥样） |
| ② golden 生成 | script_id / inputs / **expected_behavior** / result / reason | 接受/拒绝/编辑(expected_behavior, result, reason)/标假阳 |
| ③ 规则挖掘 | id / error_category / **error_reason** / if_conditions / **skill_attribution** | 接受/拒绝/编辑(error_reason, 条件, 归因skill, 置信度) |
| ④ 评估器加强 | 前:base prompt {diagnostic_rules}空 → 后:填入；诊断规则条目(从 final_rules_diagnostic.txt 按 `[Rxxx]` 解析) | toggle 单条纳入/编辑诊断文本 |
| ⑤ 定位 | case / **attributed_to** / 连带 skill | 改归属 |
| ⑥ 精选回归 | bad(按桶) ↔ good(按场景) 对照 | 移桶/标假阳 |

> 三个关键节点（冷数据 → golden 生成 → 评估器加强）做最丰满，其余三个够用即可。

---

## 持久化 + 导出

- 审阅状态存 `localStorage`（接受/拒绝/编辑值/备注），刷新不丢。
- **导出 curated JSON**：把各节点"被接受"的记录 + 编辑后的字段，下载成一份打磨后的资产包 JSON。

---

## 数据源（build_demo.py 读，全已有，不重跑）

- `data/traces/*.json` → ①
- `data/golden/golden_output.jsonl` → ②
- `data/runs/coldstart/merged/final_rules.json` → ③
- `data/runs/coldstart/merged/final_rules_diagnostic.txt` + `skills/coldstart_mining/templates/evaluator_base_prompt.txt` + `data/runs/coldstart/merged/evaluator_prompt_enhanced.md` → ④
- `data/golden/golden_localized.jsonl` → ⑤
- `data/chosen/README.md` + `badcase/` + `good/` → ⑥

---

## 落地位置

`intranet_bundle/demo/` 下：`build_demo.py` + 生成出的 `demo.html`。
内网双击 `demo.html` 即演示。

---

## 待办（对齐后开建）

- [ ] `build_demo.py`：读上述数据 → 嵌进 HTML 模板 → 生成自包含 `demo.html`
- [ ] HTML 模板：左导航 + 右面板 + 6 节点视图 + 统一审阅操作（接受/拒绝/编辑+备注）
- [ ] localStorage 持久化 + 导出 curated JSON
- [ ] 三个关键节点（①②④）做丰满，③⑤⑥ 够用
