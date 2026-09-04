# golden_review_ui — golden 审阅/修改本地网页

零依赖(标准库 `http.server`), 给 golden 调优用。**不改原始 golden**, 人工审阅结果单独落
`runs/<RUN>/golden/golden_review.jsonl`(同字段名 `expected_behavior/result/reason`, 按
`script_id` 对齐, `status=accepted|edited`)。

## 启动(pipeline 跑完 agent+golden+trace_md 后)

```cmd
cd intranet_bundle
python golden_review_ui\app.py                 :: 最新含 golden 的 run, 自动开浏览器
python golden_review_ui\app.py --run-id 20260708_151648
python golden_review_ui\app.py --port 8765 --no-browser
```

浏览器: http://127.0.0.1:8765

## 界面布局

- **左上**: trace MD(只读, 滚动) — 来自 `runs/<RUN>/md/trace_<id>.md`
- **左下**: 原始 golden(LLM 产出, 只读) — scenario / expected_behavior / result / reason
- **右**: 人工编辑区 — expected_behavior 文本框 + result 下拉(通过/部分通过/失败) + reason 文本框
  - **✓ 接受**: 认可原始、不改编(status=accepted) → 下一条
  - **💾 保存修改**: 存人工版(status=edited) → 下一条
  - 上一条 / 下一条 / 按 script_id 跳转

## 统计(/stats)

- **agent 维度**: 按最终人工 result 计 通过/部分通过/失败 各多少条 + 占比。
- **golden 维度**: LLM 原始 expected_behavior 被接受(不改)的比例 = golden LLM 质量。

## 文件

- 读: `runs/<RUN>/golden/golden_output.jsonl`(原始) + `runs/<RUN>/md/trace_<id>.md`
- 写: `runs/<RUN>/golden/golden_review.jsonl`(人工, 每条 `{script_id, status, expected_behavior, result, reason}`)
- 原始 golden_output.jsonl 全程不动, 两份都保留。

## 字段

`result` 取值: 通过 / 部分通过 / 失败 (与 golden_gen 一致)。
