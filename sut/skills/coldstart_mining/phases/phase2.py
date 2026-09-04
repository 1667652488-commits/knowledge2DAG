#!/usr/bin/env python3
"""
阶段2 v2：从全量 badcase 轨迹反推正确流程 + 缺失检查点清单（分批次迭代版）

核心变化：
- 明确输入全部是 badcase，LLM 从错误中反推检查点，而非归纳"注意事项"
- 第1批从零归纳正确链路+缺失检查点，后续批次审阅优化

用法:
    python phase2_induct_linkage.py --input trajectories.jsonl --output phase2_result.json --batch-size 10
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


# ==================== Prompt 1: 初始归纳（第1批）====================
INITIAL_SYSTEM_PROMPT = """你是一名对话式AI系统质量分析专家。你收到的**全部**轨迹都是 agent 做错事的对话（badcase），你的任务是从这些错误中反推：

1. 正确流程应该是什么样子（标准业务链路）
2. 每个步骤应该有什么**强制检查点**（不是"注意事项"，不是"改进建议"）
3. 这些轨迹中哪些检查点被违反了（缺失检查点清单）

系统理解（内化思考，不需在输出中逐条回答）：
在开始标记错误之前，先从以下维度理解这个系统：
- 系统概况：agent的角色和能力边界是什么？
- 常见场景：轨迹中出现了哪些典型的用户场景？
- 用户目标：用户通常想要达成什么？
- 常见转折：对话中经常出现哪些关键节点或转折？
- 常见陷阱：agent最容易在哪些环节出错？
- 系统缺陷模式：agent的异常行为是否可能由技术限制导致？（如工具调用失败、接口超时、MCP不通、知识库缺失、依赖的外部系统不可用等。若是技术限制导致，归纳的检查点应针对"技术限制下的兜底处理"而非"逻辑缺失"）

核心原则：
- 不要写"注意事项"或"改进建议"，要写"正确流程中的强制约束"
- 强制约束的写法标准：必须包含「触发条件」+「判定标准」+「违反时的动作」，而不是笼统的"要小心""注意检查"
- enforcement 描述的是"违反此约束后应达到的最终纠正状态"，而非"执行某个具体步骤"
- 例如：❌"注意输入金额" → ✅"校验输入金额符合业务规则，不符合则拒绝并提示原因"
- 例如：❌"金额精度要足够" → ✅"金额计算保留原始精度，禁止截断/取整/舍入"
- 例如：❌"确认后再操作" → ✅"执行前必须获得用户明确确认，未确认则拒绝执行"

根因分析原则：
- 归纳缺失检查点时，必须区分"现象"和"根因"
  - 现象是agent表现出的错误行为（如：返回了与上一批相同的推荐列表）
  - 根因是导致该行为的真正原因（如：MCP调用超时后未做兜底处理；或：确实没有做去重比对）
- 检查点应针对根因归纳，而非现象
- 如果轨迹中出现"超时""失败""降级""MCP不通"等关键词，检查点应归纳为"工具/接口不可用时的兜底处理"，而非仅仅描述表面现象

分析步骤：
1. 逐条阅读轨迹，标记 agent 在哪一步做错了——同时判断错误是"agent逻辑缺陷"还是"系统技术限制导致"
2. 对该步骤反推：如果流程正确，该步骤应该强制执行什么检查？（技术限制场景应考虑兜底处理）
3. 把相同/相近步骤的检查点合并，形成通用检查点清单
4. 归纳标准业务链路（步骤名称 + 每个步骤的强制检查点）

重要提示：
- 如果同一 conversation_id 出现多条轨迹，说明该场景在此批次中高频出现，请在 missing_checkpoints_list 的 severity 中标记为"高"
- 如果不同轨迹呈现不同的错误模式，请分别列出独立的检查点，而非过度合并
- 宁可检查点多一些也不要遗漏，后续阶段会做类别聚合和筛选

输出格式（严格 JSON）：
{
  "analysis_summary": "对这批 badcase 的总体分析（如'主要集中在确认环节缺失和金额精度问题'）",
  "linkage": [
    {
      "step_id": 1,
      "step_name": "步骤名称（通用语言）",
      "agent_action": "agent在此步骤做什么",
      "mandatory_checkpoints": [
        {
          "checkpoint_id": "CP001",
          "description": "检查点描述（强制约束，不是注意事项）",
          "enforcement": "如果违反应该怎么做（如：拒绝并提示用户）"
        }
      ]
    }
  ],
  "missing_checkpoints_list": [
    {
      "checkpoint_id": "CP001",
      "description": "检查点描述（强制约束）",
      "violation_pattern": "在轨迹中如何体现被违反（如：agent未确认直接执行购买）",
      "affected_trajectories": ["conv-001", "conv-003"],
      "severity": "高/中/低"
    }
  ],
  "recommendations_for_phase3": "给阶段3的提示：哪些缺失检查点应该聚合成同一类别"
}"""


# ==================== Prompt 2: 审阅优化（后续批次）====================
REFINE_SYSTEM_PROMPT = """你是一名对话式AI系统质量分析专家。你已有前序批次归纳出的"正确流程+缺失检查点清单"，现在需要用新的 badcase 轨迹对其进行审阅和优化。

审阅任务：
1. 新轨迹中是否有前序分析未覆盖的错误模式？
2. 是否有新的缺失检查点需要补充？
3. 已有检查点的描述是否需要修正（更准确地反映"强制约束"而非"注意事项"）？
4. 已有检查点是否误将"系统技术限制"归为"agent逻辑缺陷"？（如：接口超时后返回相同数据，不应归为"去重逻辑缺失"，应归为"工具不可用时的兜底处理缺失"）
5. 更新缺失检查点清单中的 affected_trajectories

修正原则（强制约束 vs 注意事项的示例）：
- ❌ 错误写法："注意输入金额"（笼统，无判定标准）
- ✅ 正确写法："校验输入金额符合业务规则，不符合则拒绝并提示原因"
- ❌ 错误写法："金额精度要足够"（笼统，无判定标准）
- ✅ 正确写法："金额计算保留原始精度，禁止截断/取整/舍入"
- ❌ 错误写法："确认后再操作"（笼统，无判定标准）
- ✅ 正确写法："执行前必须获得用户明确确认，未确认则拒绝执行"

根因审查原则：
- 审阅已有检查点时，判断其归纳的根因是否准确
- 如果检查点描述的"缺失逻辑"实际是由技术限制导致（如MCP超时、接口降级、工具调用失败），应修正为针对"技术限制下的兜底处理"
- 例如：❌"换一批时未做去重校验"（只看到结果重复的表象）→ ✅"工具调用失败时缺少兜底话术告知用户当前无法操作"（看穿根因）

输出格式（严格 JSON）：
{
  "review_notes": "本轮审阅的主要发现（如'补充了异常处理环节'、'修正了XX检查点描述为强制约束'）",
  "analysis_summary": "更新后的总体分析",
  "linkage": ["优化后的完整标准链路（步骤+强制检查点）"],
  "missing_checkpoints_list": ["更新后的缺失检查点清单（含新增和修正）"],
  "recommendations_for_phase3": "给阶段3的更新提示"
}"""


def load_trajectories(path: str, dedup: bool = False) -> List[Dict]:
    """加载轨迹数据

    Args:
        path: 输入文件或目录路径
        dedup: 是否按 conversation_id 去重（默认关闭，因为重复轨迹代表场景高频出现，需要重点关注）
    """
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

    # 按 conversation_id 去重（可选）：同一对话可能因多次运行产生不同时间戳的文件
    # 默认不去重，因为重复轨迹代表场景高频出现，需要重点关注
    if dedup:
        seen_ids = set()
        deduped = []
        dup_count = 0
        for t in trajectories:
            cid = t.get('conversation_id', None)
            if cid is None:
                deduped.append(t)
            elif cid not in seen_ids:
                seen_ids.add(cid)
                deduped.append(t)
            else:
                dup_count += 1
        if dup_count > 0:
            print(f"去重：发现 {dup_count} 条重复轨迹（相同 conversation_id），已去除")
        trajectories = deduped

    return trajectories


def format_trajectory_text(trajectory: Dict) -> str:
    """将单条轨迹格式化为文本"""
    lines = []
    conv_id = trajectory.get('conversation_id', 'unknown')
    script_id = trajectory.get('script_id', '')
    lines.append(f"【对话ID: {conv_id} (剧本 {script_id})】")
    for idx, h in enumerate(trajectory.get('messages', [])):
        role = {'user':'用户','assistant':'Agent','tool':'工具'}.get(h.get('role',''), h.get('role','?'))
        turn = idx + 1
        content = h.get('content', '')[:500]
        lines.append(f"  第{turn}轮 [{role}]: {content}")
    lines.append("")
    return "\n".join(lines)


def format_batch(trajectories: List[Dict], batch_num: int, total_batches: int) -> str:
    """格式化一批轨迹"""
    header = f"=== 第 {batch_num}/{total_batches} 批（共 {len(trajectories)} 条轨迹） ===\n"
    texts = [format_trajectory_text(t) for t in trajectories]
    return header + "\n".join(texts)


def parse_llm_json(raw: str) -> Dict:
    """从LLM回复中提取JSON"""
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


def induct_initial_linkage(trajectories: List[Dict], max_retries: int = 3) -> Dict:
    """第1批：从零生成正确链路 + 缺失检查点（含重试）"""
    batch_text = format_batch(trajectories, 1, 1)

    prompt = f"""请分析以下 {len(trajectories)} 条 badcase 轨迹。所有轨迹都是 agent 做错的情况。

要求：
1. 逐条分析 agent 在哪一步做错了
2. 从错误反推：正确流程在该步骤应该有什么强制检查点
3. 归纳标准业务链路（步骤 + 每个步骤的强制检查点）
4. 产出缺失检查点清单（哪些轨迹违反了哪个检查点）

特别注意检查点描述规范（强制约束 vs 注意事项）：
- ❌ 错误写法："提示客户不要输入负值金额"
- ✅ 正确写法："校验 buy_amount > 0，若为零或负数则拒绝并提示用户重新输入"
- ❌ 错误写法："金额不要四舍五入"
- ✅ 正确写法："缺口金额按原始精度保留到分位，禁止截断或取整"
- ❌ 错误写法："提高计算精度"
- ✅ 正确写法："未确认产品是否支持日复利时，应婉拒收益计算请求"

=== 轨迹数据 ===
{batch_text}

请严格按照 system prompt 的 JSON 格式输出。"""

    messages = [
        {"role": "system", "content": INITIAL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]

    print(f"【第1批】基于 {len(trajectories)} 条 badcase 从零归纳正确链路...")

    for attempt in range(1, max_retries + 1):
        raw = call_llm(messages)
        result = parse_llm_json(raw)
        if not result.get('parse_error'):
            if attempt > 1:
                print(f"  ✓ 第 {attempt} 次重试成功")
            return result
        print(f"  ⚠ 第1批第 {attempt}/{max_retries} 次调用失败: {raw[:80]}...")

    print(f"  ✗ 第1批 {max_retries} 次重试均失败")
    return result  # 返回最后一次的失败结果


def refine_linkage(existing_result: Dict, new_batch: List[Dict], batch_num: int,
                    total_batches: int, max_retries: int = 3) -> Dict:
    """后续批次：审阅并优化（含重试）"""
    existing_text = json.dumps(existing_result, ensure_ascii=False, indent=2)
    new_batch_text = format_batch(new_batch, batch_num, total_batches)
    
    prompt = f"""你已有前序批次归纳出的"正确流程+缺失检查点清单"，现在需要用新的 badcase 轨迹对其进行审阅和优化。

=== 当前分析结果 ===
{existing_text}

=== 新批次轨迹数据（第{batch_num}/{total_batches}批，共{len(new_batch)}条）===
{new_batch_text}

请审阅：
1. 新轨迹中是否有前序分析未覆盖的错误模式？
2. 是否有新的缺失检查点需要补充？
3. 已有检查点的描述是否需要修正（更准确地反映"强制约束"而非"注意事项"）？
4. 更新缺失检查点清单中的 affected_trajectories

特别注意检查点描述规范（强制约束 vs 注意事项）：
- ❌ 错误写法："注意输入金额"（笼统，无判定标准）
- ✅ 正确写法："校验输入金额符合业务规则，不符合则拒绝并提示原因"
- ❌ 错误写法："金额精度要足够"（笼统，无判定标准）
- ✅ 正确写法："金额计算保留原始精度，禁止截断/取整/舍入"
- ❌ 错误写法："确认后再操作"（笼统，无判定标准）
- ✅ 正确写法："执行前必须获得用户明确确认，未确认则拒绝执行"

请输出优化后的完整分析结果（严格 JSON 格式）。"""
    
    messages = [
        {"role": "system", "content": REFINE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]
    
    print(f"【第{batch_num}批】基于 {len(new_batch)} 条 badcase 审阅优化...")

    for attempt in range(1, max_retries + 1):
        raw = call_llm(messages)
        result = parse_llm_json(raw)
        if not result.get('parse_error'):
            if attempt > 1:
                print(f"  ✓ 第 {attempt} 次重试成功")
            return result
        print(f"  ⚠ 第{batch_num}批第 {attempt}/{max_retries} 次调用失败: {raw[:80]}...")

    print(f"  ✗ 第{batch_num}批 {max_retries} 次重试均失败")
    return result  # 返回最后一次的失败结果


def induct_linkage_batched(trajectories: List[Dict], batch_size: int, output_dir: str) -> Dict:
    """
    分批次归纳：第1批从零归纳，后续批次审阅优化
    每批调用失败时重试最多3次；全部失败则保留上轮成功结果而不覆盖
    """
    total = len(trajectories)
    total_batches = (total + batch_size - 1) // batch_size

    print(f"共 {total} 条 badcase 轨迹，分 {total_batches} 批处理，每批最多 {batch_size} 条")
    print("=" * 60)

    current_result = None
    last_valid_result = None  # 最近一次成功的有效结果

    for batch_num in range(1, total_batches + 1):
        start_idx = (batch_num - 1) * batch_size
        end_idx = min(batch_num * batch_size, total)
        batch = trajectories[start_idx:end_idx]

        if batch_num == 1:
            current_result = induct_initial_linkage(batch)
        else:
            current_result = refine_linkage(current_result, batch, batch_num, total_batches)

        # 判断本轮是否成功
        is_valid = isinstance(current_result, dict) and not current_result.get('parse_error')

        if is_valid:
            last_valid_result = current_result
        else:
            # 本轮失败，保留上轮成功结果
            if last_valid_result is not None:
                print(f"  ⚠ 第{batch_num}批失败，保留前序有效结果（{len(last_valid_result.get('missing_checkpoints_list', []))} 个检查点）")
                current_result = last_valid_result
            else:
                print(f"  ⚠ 第{batch_num}批失败且无前序有效结果，继续尝试后续批次")

        # 保存中间结果（无论成功失败都保存，便于诊断）
        intermediate_path = os.path.join(output_dir, f"phase2_batch_{batch_num}.json")
        with open(intermediate_path, 'w', encoding='utf-8') as f:
            json.dump({
                "batch_num": batch_num,
                "total_batches": total_batches,
                "trajectories_used": end_idx,
                "result": current_result
            }, f, ensure_ascii=False, indent=2)

        review_notes = current_result.get('review_notes', '') if isinstance(current_result, dict) else ''
        if review_notes and review_notes != '无变化':
            print(f"  本轮优化: {review_notes[:100]}")

        # 打印当前缺失检查点数量
        missing = current_result.get('missing_checkpoints_list', []) if isinstance(current_result, dict) else []
        if missing:
            print(f"  当前缺失检查点: {len(missing)} 项")

        # 检查 LLM 输出格式是否符合预期
        linkage = current_result.get('linkage', []) if isinstance(current_result, dict) else []
        if linkage and isinstance(linkage[0], str):
            print(f"  ⚠ 提示: LLM 返回的 linkage 步骤为纯文本而非对象格式，建议优化 Prompt 强调每个步骤必须为 JSON 对象")

    # 最终包装：优先使用有效结果
    final_output = last_valid_result if last_valid_result is not None else current_result
    final_result = {
        "meta": {
            "total_trajectories": total,
            "batch_size": batch_size,
            "total_batches": total_batches,
            "processing_mode": "batched_iterative_refinement",
            "all_are_badcase": True
        },
        "phase2_output": final_output
    }

    return final_result


def print_summary(result: Dict):
    """打印摘要"""
    data = result.get('phase2_output', result)

    print("\n" + "=" * 60)
    print("【Phase 2 分析结果】")
    print("=" * 60)

    summary = data.get('analysis_summary', '') if isinstance(data, dict) else ''
    if summary:
        print(f"\n分析摘要: {summary}")
    
    linkage = data.get('linkage', []) if isinstance(data, dict) else []
    print(f"\n标准链路步骤: {len(linkage)}")
    for i, step in enumerate(linkage, 1):
        if isinstance(step, str):
            print(f"  步骤 {i}: {step}")
        elif isinstance(step, dict):
            sid = step.get('step_id', i)
            name = step.get('step_name', '')
            cps = step.get('mandatory_checkpoints', [])
            print(f"  步骤 {sid}: {name} ({len(cps)}个检查点)")
        else:
            print(f"  步骤 {i}: {step}")
    
    missing = data.get('missing_checkpoints_list', []) if isinstance(data, dict) else []
    print(f"\n缺失检查点清单 ({len(missing)}项):")
    for cp in missing:
        if isinstance(cp, dict):
            cpid = cp.get('checkpoint_id', '')
            desc = cp.get('description', '')
            sev = cp.get('severity', '')
            affected = len(cp.get('affected_trajectories', []))
            print(f"  {cpid} [{sev}] {desc[:60]}... (影响{affected}条轨迹)")
        else:
            print(f"  {cp}")
    
    rec = data.get('recommendations_for_phase3', '') if isinstance(data, dict) else ''
    if rec:
        print(f"\nPhase3 建议: {rec[:150]}")
    
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="阶段2 v2：分批次从 badcase 反推正确流程+缺失检查点")
    parser.add_argument("--input", type=str, required=True, help="输入轨迹文件或目录")
    parser.add_argument("--output", type=str, required=True, help="最终输出 JSON 文件")
    parser.add_argument("--batch-size", type=int, default=10, help="每批处理轨迹数（默认 10）")
    parser.add_argument("--intermediate-dir", type=str, default="phase2_intermediate", help="中间结果保存目录")
    parser.add_argument("--dedup", action="store_true", help="开启按 conversation_id 去重（默认不去重，重复轨迹代表场景高频出现）")
    args = parser.parse_args()

    # 加载轨迹
    trajectories = load_trajectories(args.input, dedup=args.dedup)
    print(f"加载了 {len(trajectories)} 条 badcase 轨迹")
    
    if len(trajectories) == 0:
        print("错误: 未找到轨迹数据")
        return
    
    # 创建中间结果目录
    os.makedirs(args.intermediate_dir, exist_ok=True)
    
    # 分批次归纳
    final_result = induct_linkage_batched(
        trajectories, 
        batch_size=args.batch_size,
        output_dir=args.intermediate_dir
    )
    
    # 保存最终结果
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(final_result, f, ensure_ascii=False, indent=2)
    
    # 打印摘要
    print_summary(final_result)
    
    print(f"\n最终结果已保存: {args.output}")
    print(f"中间结果目录: {args.intermediate_dir}/")
    print("提示: 请review缺失检查点清单，确认描述是'强制约束'而非'注意事项'，然后进入阶段3")


if __name__ == "__main__":
    main()
