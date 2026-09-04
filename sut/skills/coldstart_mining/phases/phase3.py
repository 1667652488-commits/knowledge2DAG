#!/usr/bin/env python3
"""
阶段3：从缺失检查点清单归纳 badcase 类别 + 拆解可判定检查点

核心原则：
- 类别由 LLM 从数据中自由归纳，不预设任何类别名称
- 每个类别必须包含一组二值检查点（True/False 可判定）
- 检查点必须是客观事实，不能含主观评价

输入：
  - phase2_result.json（含 missing_checkpoints_list）
  - 原始轨迹数据（用于验证检查点可判定性）

输出：
  - categories.json：类别定义 + 检查点清单

用法: phase3_induct_categories.py [-h] --phase2-result PHASE2_RESULT --trajectories TRAJECTORIES [--output OUTPUT] [--batch-size BATCH_SIZE]
必填参数:
  --phase2-result   phase2 输出文件 (phase2_result.json)
  --trajectories    原始轨迹数据文件或目录

可选参数:
  --output          输出 JSON 文件 (默认: phase3_categories.json)
  --batch-size      每批处理的检查点数（目前固定2批：归纳+审查） (默认: 15)
  -h, --help        显示帮助信息

  
# 基本用法示例
python phase3_induct_categories.py --phase2-result phase2_result.json --trajectories input_trace/0611v1/chosen/

# 自定义输出文件名
python phase3_induct_categories.py --phase2-result phase2_result.json --trajectories input_trace/0611v1/chosen/ --output phase3_result.json

# phase2 输出文件名不同时
python phase3_induct_categories.py --phase2-result phase1output.json --trajectories input_trace/0611v1/chosen/

"""

import json
import argparse
import os
from pathlib import Path
from typing import List, Dict

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
from common.llm_client import call_llm


# ==================== Prompt: 自由归纳类别 ====================
PHASE3_SYSTEM_PROMPT = """你是一个对话式 AI 系统的质量分析专家。你的任务是从「缺失检查点清单」中归纳出 badcase 类别体系。

核心原则：
1. 类别名称从数据中自然生长出来，不要套用任何预设分类框架
2. 每个类别必须对应一组「二值检查点」——对任意单条轨迹，每个检查点都能明确回答 True（通过）或 False（违反）
3. 检查点描述必须是客观事实，禁止出现"态度不好""回答敷衍"等主观评价

归纳步骤：
1. 阅读所有缺失检查点，找出它们背后的「错误模式」共性
2. 把共性相近的缺失检查点聚成一类，给类别起一个精准的名字（如"确认环节缺失"而非"流程问题"）
3. 为每个类别拆解出 1-3 个二值检查点，确保每条轨迹能被这些检查点客观判定
4. 输出类别定义 + 检查点 + 判定示例

检查点描述规范：必须可二值判定，禁止主观评价

判定标准编写原则（重要）：
- 聚焦"最终状态"而非"具体步骤"：
  描述「最终应该达成的正确状态」，而非「中间应该执行什么动作」
- 从原则层面概括，不陷入中间细节：
  不是"agent应该做X步骤"，而是"确保最终X条件被满足"
- 说明最后一轮应该达成什么目标
- 聚焦安全、合规、用户体验等核心目标
- 用"在...前提下完成...""确保...""尊重..."等概括性表述

判定标准编写示例：
- ❌ 步骤式："agent是否在下一轮回复中将金额清洗为纯数字格式并回显"
- ✅ 最终状态式："确保用户在清晰获知标准化金额的前提下予以确认，且被告知后续应使用标准格式"
- ❌ 步骤式："agent是否向用户询问了确认"
- ✅ 最终状态式："确保用户在明确知晓产品名称和金额的前提下主动确认，agent未确认则不执行"

输出格式（严格 JSON）：
{
  "induction_summary": "归纳总体思路摘要",
  "categories": [
    {
      "category_id": "CAT001",
      "category_name": "类别名称（从数据长出来的）",
      "description": "该类别的精确定义：什么情况下属于此类别",
      "source_checkpoints": ["CP001", "CP003"],
      "binary_checkpoints": [
        {
          "checkpoint_id": "CHK001",
          "description": "检查点描述（True/False 可判定）",
          "judgment_criteria": "如何判定 True/False 的具体标准",
          "evidence_location": "在轨迹的哪个位置找证据（如：最后一条 agent 回复）",
          "positive_example_trajectory": "conv-001",
          "negative_example_trajectory": "conv-002"
        }
      ],
      "estimated_coverage": "预计覆盖多少比例轨迹",
      "severity": "高/中/低"
    }
  ],
  "uncategorized_checkpoints": ["CP005"],
  "coverage_report": {
    "total_trajectories": 50,
    "covered_by_categories": 48,
    "uncategorized": 2,
    "notes": "未覆盖的轨迹说明"
  }
}"""


def load_json(path: str) -> Dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_trajectories(path: str) -> List[Dict]:
    trajectories = []
    if os.path.isfile(path):
        if path.endswith('.jsonl'):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        trajectories.append(json.loads(line))
        elif path.endswith('.json'):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    trajectories = data
                elif isinstance(data, dict) and 'history' in data:
                    trajectories = [data]
                else:
                    trajectories = [data]
    elif os.path.isdir(path):
        for json_file in sorted(Path(path).glob('*.json')):
            with open(json_file, 'r', encoding='utf-8') as f:
                trajectories.append(json.load(f))
    return trajectories


def format_checkpoint_list(checkpoints: List[Dict]) -> str:
    lines = []
    for cp in checkpoints:
        cpid = cp.get('checkpoint_id', '?')
        desc = cp.get('description', '')
        pattern = cp.get('violation_pattern', '')
        sev = cp.get('severity', '')
        affected = cp.get('affected_trajectories', [])
        lines.append(f"- {cpid} [{sev}] {desc}")
        lines.append(f"  违反模式: {pattern}")
        lines.append(f"  影响轨迹: {', '.join(affected[:5])}{'...' if len(affected) > 5 else ''}")
        lines.append("")
    return "\n".join(lines)


def format_sample_trajectories(trajectories: List[Dict], max_samples: int = 5) -> str:
    """为类别归纳提供代表性轨迹样本"""
    samples = trajectories[:max_samples]
    lines = []
    for traj in samples:
        conv_id = traj.get('conversation_id', 'unknown')
        script_id = traj.get('script_id', '')
        lines.append(f"=== 轨迹 {conv_id} (剧本 {script_id}) ===")
        for idx, h in enumerate(traj.get('messages', [])[:6]):
            role = {'user':'用户','assistant':'Agent','tool':'工具'}.get(h.get('role',''), h.get('role','?'))
            turn = idx + 1
            content = h.get('content', '')[:400]
            lines.append(f"  第{turn}轮 [{role}]: {content}")
        lines.append("")
    return "\n".join(lines)


def parse_llm_json(raw: str) -> Dict:
    import re
    try:
        return json.loads(raw)
    except:
        pass
    code_blocks = re.findall(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if not code_blocks:
        code_blocks = re.findall(r'```\s*(.*?)\s*```', raw, re.DOTALL)
    for block in code_blocks:
        try:
            return json.loads(block)
        except:
            pass
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            pass
    return {"parse_error": True, "raw": raw}


def induct_categories(checkpoints: List[Dict], trajectories: List[Dict],
                      batch_size: int = 15,
                      min_categories: int = 0, max_categories: int = 0) -> Dict:
    """
    分批次归纳类别：
    - 第一批：从全部检查点归纳初始类别框架
    - 后续：逐批细化检查点描述，确保二值可判定
    """

    total_cp = len(checkpoints)
    print(f"共 {total_cp} 个缺失检查点，开始归纳类别...")
    print("=" * 60)

    # 第一批：用全部检查点做初始归纳
    cp_text = format_checkpoint_list(checkpoints)
    sample_text = format_sample_trajectories(trajectories, max_samples=5)

    # 构造类别数量约束文本
    category_count_hint = ""
    if min_categories > 0 and max_categories > 0:
        category_count_hint = f"\n5. 请归纳出 {min_categories}~{max_categories} 个类别（不要过度合并，宁可细一些也不要遗漏）"
    elif min_categories > 0:
        category_count_hint = f"\n5. 请至少归纳出 {min_categories} 个类别（不要过度合并，宁可细一些也不要遗漏）"
    elif max_categories > 0:
        category_count_hint = f"\n5. 请归纳出不超过 {max_categories} 个类别"

    prompt_batch1 = f"""请从以下 {total_cp} 个缺失检查点中归纳 badcase 类别体系。

=== 缺失检查点清单 ===
{cp_text}

=== 代表性轨迹样本 ===
{sample_text}

要求：
1. 类别名称必须从数据自然生长，不要套用预设框架
2. 每个类别拆解出 1-3 个二值检查点（True/False 可客观判定）
3. 给出每个检查点的判定标准、证据位置、正负例轨迹
4. 所有检查点描述禁止主观评价，必须是客观事实{category_count_hint}
6. 如果不同检查点反映不同的错误模式，请分为不同类别，而非过度合并

请严格按照 system prompt 的 JSON 格式输出。"""
    
    messages = [
        {"role": "system", "content": PHASE3_SYSTEM_PROMPT},
        {"role": "user", "content": prompt_batch1}
    ]
    
    print("【第1/2批】归纳初始类别框架...")
    raw1 = call_llm(messages)
    result = parse_llm_json(raw1)
    
    # 第二批：审查检查点可判定性
    categories = result.get('categories', [])
    if categories:
        cat_summary = []
        for cat in categories:
            cat_summary.append(f"- {cat.get('category_id', '?')}: {cat.get('category_name', '')}")
            for chk in cat.get('binary_checkpoints', []):
                cat_summary.append(f"  检查点 {chk.get('checkpoint_id', '?')}: {chk.get('description', '')[:80]}")
        
        cat_summary_text = "\n".join(cat_summary)
        review_prompt = f"""请审查以下已归纳的类别和检查点，重点审查「可判定性」：

=== 已归纳类别 ===
{cat_summary_text}

审查要求：
1. 每个检查点是否真的能对单条轨迹回答 True/False？
2. 判定标准是否足够具体，不同 reviewer 不会给出不同结论？
3. 证据位置描述是否清晰（知道去轨迹的哪里找证据）？
4. 检查点描述是否都是客观事实，没有主观评价词汇？

如有问题，请修正检查点描述或重新归类。
请输出审查后的最终类别体系（严格 JSON 格式）。"""
        
        messages2 = [
            {"role": "system", "content": PHASE3_SYSTEM_PROMPT},
            {"role": "user", "content": prompt_batch1},
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False, indent=2)[:3000]},
            {"role": "user", "content": review_prompt}
        ]
        
        print("【第2/2批】审查检查点可判定性...")
        raw2 = call_llm(messages2)
        result = parse_llm_json(raw2)
    
    return result


def print_summary(result: Dict):
    categories = result.get('categories', [])
    uncategorized = result.get('uncategorized_checkpoints', [])
    coverage = result.get('coverage_report', {})
    
    print("\n" + "=" * 60)
    print("【Phase 3 类别归纳结果】")
    print("=" * 60)
    
    summary = result.get('induction_summary', '')
    if summary:
        print(f"\n归纳思路: {summary}")
    
    print(f"\n类别数量: {len(categories)}")
    for cat in categories:
        cid = cat.get('category_id', '?')
        name = cat.get('category_name', '')
        sev = cat.get('severity', '')
        chks = cat.get('binary_checkpoints', [])
        src = cat.get('source_checkpoints', [])
        print(f"\n  {cid} [{sev}] {name}")
        print(f"    来源检查点: {', '.join(src)}")
        for chk in chks:
            chk_id = chk.get('checkpoint_id', '?')
            desc = chk.get('description', '')
            print(f"    检查点 {chk_id}: {desc[:70]}...")
    
    if uncategorized:
        print(f"\n未归类检查点: {', '.join(uncategorized)}")
    
    if coverage:
        total = coverage.get('total_trajectories', 0)
        covered = coverage.get('covered_by_categories', 0)
        print(f"\n覆盖报告: {covered}/{total} 条轨迹被类别覆盖")
        notes = coverage.get('notes', '')
        if notes:
            print(f"  备注: {notes}")
    
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="阶段3：从缺失检查点归纳 badcase 类别")
    parser.add_argument("--phase2-result", type=str, required=True,
                        help="phase2 输出文件 (phase2_result.json)")
    parser.add_argument("--trajectories", type=str, required=True,
                        help="原始轨迹数据文件或目录")
    parser.add_argument("--output", type=str, default="phase3_categories.json",
                        help="输出 JSON 文件")
    parser.add_argument("--batch-size", type=int, default=15,
                        help="每批处理的检查点数（目前固定2批：归纳+审查）")
    parser.add_argument("--min-categories", type=int, default=0,
                        help="最少归纳类别数（0=不限制，建议 max(3, checkpoint数//3)）")
    parser.add_argument("--max-categories", type=int, default=0,
                        help="最多归纳类别数（0=不限制，建议 max(8, checkpoint数//1.5)）")
    args = parser.parse_args()

    # 加载 phase2 结果
    phase2_data = load_json(args.phase2_result)
    phase2_output = phase2_data.get('phase2_output', phase2_data)
    checkpoints = phase2_output.get('missing_checkpoints_list', [])

    # 加载轨迹
    trajectories = load_trajectories(args.trajectories)

    # 自动推荐类别数
    min_cat = args.min_categories
    max_cat = args.max_categories
    if min_cat == 0 and max_cat == 0:
        # 根据检查点数量自动推荐
        n_cp = len(checkpoints)
        if n_cp >= 3:
            min_cat = max(3, n_cp // 3)
            max_cat = max(8, int(n_cp / 1.5))
            print(f"自动推荐类别数范围: {min_cat}~{max_cat}（基于 {n_cp} 个检查点）")
        else:
            min_cat = 0
            max_cat = 0

    print(f"加载了 {len(checkpoints)} 个缺失检查点，{len(trajectories)} 条轨迹")

    if len(checkpoints) == 0:
        print("错误: 未找到缺失检查点数据，请先运行阶段2")
        return

    result = induct_categories(checkpoints, trajectories, batch_size=args.batch_size,
                               min_categories=min_cat, max_categories=max_cat)
    
    # 包装最终输出
    final = {
        "meta": {
            "source_phase2": args.phase2_result,
            "source_trajectories": args.trajectories,
            "total_checkpoints_input": len(checkpoints),
            "total_trajectories": len(trajectories)
        },
        "phase3_output": result
    }
    
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    
    print_summary(result)
    
    print(f"\n结果已保存: {args.output}")
    print("提示: 请review类别名称是否自然、检查点是否可二值判定，然后进入阶段4（特征提取）")


if __name__ == "__main__":
    main()
