#!/usr/bin/env python3
"""
阶段6：规则语言化 + 排序筛选

功能：
1. 将 rules.json 转化为自然语言规则文本（类似人工归纳规则.txt），
   方便作为评估器（checker）的提示词输入
2. 对全部规则按合理性、重要性、出现频率三维排序
3. 筛掉排名垫底的规则，保留高质量规则

输入：
  - rules.json（Phase5 输出）
  - phase3output.json（类别定义 + severity）
  - 原始轨迹数据（用于频率统计和 LLM 审查）
  - skill markdown 文件或目录（用于 LLM 审查）

输出：
  - rules_natural_language.txt  — 自然语言规则文本
  - rules_ranked.json           — 排序后的规则（含得分、筛选结果）

用法：
  python phase6_rule_transform.py \
    --rules rules.json \
    --categories phase3output.json \
    --trajectories input_trace/0611v1/chosen/ \
    --skills skills_2b_checked \
    --output rules_natural_language.txt \
    --output-json rules_ranked.json \
    --top-k-rules 8 \
    --min-score 0.3 \
    --drop-bottom 0 \
    --weights 0.4,0.3,0.3
"""

import json
import argparse
import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter, defaultdict
from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
from common.llm_client import call_llm


# ==================== 数据模型 ====================

@dataclass
class RankedRule:
    rule_id: str
    error_category: str
    error_reason: str
    if_conditions: List[Dict[str, Any]]
    then_category: str
    skill_attribution: Dict[str, Any]
    supporting_trajectories: List[str]
    few_shots: Dict[str, List[Dict]]
    confidence: float
    # Phase6 新增字段
    score_rationality: float = 0.0   # 合理性得分 (0-1)
    score_importance: float = 0.0    # 重要性得分 (0-1)
    score_frequency: float = 0.0     # 出现频率得分 (0-1)
    score_total: float = 0.0         # 综合得分 (0-1)
    rank: int = 0                    # 排名
    rejected: bool = False           # 是否被筛掉
    expected_behavior: str = ""      # 正确行为指引（在xx情况下，agent应该怎么做）


# ==================== 数据加载 ====================

def load_rules(path: str) -> List[Dict]:
    """加载 rules.json"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    return [data]


def load_categories(path: str) -> Dict:
    """加载 phase3output.json，提取类别定义"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    phase3 = data.get('phase3_output', data)
    return phase3


def load_trajectories(path: str) -> List[Dict]:
    """加载原始轨迹"""
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
                trajectories = data if isinstance(data, list) else [data]
    elif os.path.isdir(path):
        for json_file in sorted(Path(path).glob('*.json')):
            with open(json_file, 'r', encoding='utf-8') as f:
                trajectories.append(json.load(f))
    return trajectories


def load_skills(path: str) -> List[Dict]:
    """加载 skill markdown 文件"""
    skills = []
    p = Path(path)
    if p.is_file():
        files = [p]
    elif p.is_dir():
        files = sorted(p.glob('*.md')) + sorted(p.glob('*.markdown'))
    else:
        return []

    for f in files:
        content = f.read_text(encoding='utf-8')
        skills.append({
            'name': f.stem,
            'content': content,
            'summary': content[:2000]
        })
    return skills


# ==================== 频率得分计算 ====================

def compute_frequency_scores(rules: List[Dict], total_trajectories: int) -> Dict[str, float]:
    """计算每条规则的出现频率得分

    基于 supporting_trajectories 数量和 confidence
    """
    scores = {}
    for rule in rules:
        rule_id = rule.get('id', '')
        supporting = rule.get('supporting_trajectories', [])
        # 支撑轨迹数 / 总轨迹数 = 触发率
        trigger_rate = len(supporting) / max(total_trajectories, 1)
        # 结合原始 confidence
        confidence = rule.get('confidence', 0.5)
        # 频率得分 = 触发率的平方根（避免线性放大） × confidence
        freq_score = min(1.0, (trigger_rate ** 0.5) * 1.5) * confidence
        scores[rule_id] = freq_score

    return scores


# ==================== 重要性得分计算 ====================

def compute_importance_scores(rules: List[Dict], categories_data: Dict) -> Dict[str, float]:
    """计算每条规则的重要性得分

    基于 severity 和 skill_attribution 中的最高置信度
    """
    # 构建类别 severity 映射
    cat_severity = {}
    for cat in categories_data.get('categories', []):
        cat_id = cat.get('category_id', '')
        sev = cat.get('severity', '中')
        cat_severity[cat_id] = sev

    # severity → 数值
    sev_map = {'高': 1.0, '中': 0.6, '低': 0.3}

    scores = {}
    for rule in rules:
        rule_id = rule.get('id', '')
        error_category = rule.get('error_category', '')
        # 从 "CAT001-重复推荐未去重" 中提取 cat_id
        cat_id = error_category.split('-')[0] if '-' in error_category else ''

        # severity 得分
        sev = cat_severity.get(cat_id, '中')
        sev_score = sev_map.get(sev, 0.6)

        # skill_attribution 最高置信度
        top3 = rule.get('skill_attribution', {}).get('top3', [])
        if top3:
            max_skill_conf = max(item.get('confidence', 0.0) for item in top3 if isinstance(item, dict))
        else:
            max_skill_conf = 0.0

        # 重要性 = severity权重 × 0.5 + skill_max_conf × 0.5
        importance = sev_score * 0.5 + max_skill_conf * 0.5
        scores[rule_id] = importance

    return scores


# ==================== 合理性得分（LLM 对抗性审查） ====================

RATIONALITY_SYSTEM_PROMPT = """你是一个对话式 AI 系统的质量规则审查专家。

你将对一条 badcase 规则进行对抗性审查，评估其合理性。你需要从以下角度进行审查：

1. 逻辑自洽性：error_reason 是否与 if_conditions 因果一致？
2. IF 可判定性：if_conditions 中描述的触发条件是否可以用自然语言精准判别？
3. THEN 合理性：then_category 的结论是否是合理的错误归类？
4. 规则冗余性：这条规则是否与其他规则过多重叠？
5. 规则完备性：是否遗漏了重要场景？

你将收到：
- 规则内容
- 相关的对话轨迹片段
- 系统 Skill 提示词摘要

请输出以下 JSON：
{
  "rationality_score": 0.0-1.0,
  "issues": ["发现的问题1", "发现的问题2"],
  "suggestions": ["建议1", "建议2"]
}

评分标准：
- 0.8-1.0：规则逻辑清晰，IF 可判定，THEN 合理，无冗余
- 0.6-0.8：规则基本合理，但有小瑕疵（如 IF 条件可更精确）
- 0.4-0.6：规则有中等问题（如 error_reason 与 IF 条件不完全对应）
- 0.2-0.4：规则有严重问题（如 IF 条件过于宽泛或狭窄）
- 0.0-0.2：规则逻辑不通或完全不可用"""


def compute_rationality_scores(rules: List[Dict],
                                trajectories: List[Dict],
                                skills: List[Dict],
                                max_retries: int = 2) -> Dict[str, Tuple[float, List[str]]]:
    """用 LLM 对每条规则做对抗性审查，返回 {rule_id: (score, issues)}"""

    traj_map = {t.get('conversation_id', ''): t for t in trajectories}
    skill_names = [s['name'] for s in skills]
    skill_summaries = "\n".join(
        f"Skill: {s['name']}\n{s['summary'][:500]}" for s in skills
    )

    scores = {}
    for rule in rules:
        rule_id = rule.get('id', '')

        # 提取支撑轨迹片段
        supporting_ids = rule.get('supporting_trajectories', [])
        traj_snippets = []
        for sid in supporting_ids[:3]:
            traj = traj_map.get(sid)
            if traj:
                history = traj.get('messages', [])[-4:]
                snippet_lines = [f"  轨迹{sid}:"]
                for h in history:
                    role = {'user':'用户','assistant':'Agent','tool':'工具'}.get(h.get('role',''), h.get('role','?'))
                    content = h.get('content', '')[:200]
                    snippet_lines.append(f"    [{role}]: {content}")
                traj_snippets.append("\n".join(snippet_lines))

        traj_text = "\n".join(traj_snippets) if traj_snippets else "（无支撑轨迹）"

        # 构造审查 prompt
        prompt = f"""请审查以下 badcase 规则的合理性：

=== 规则 ===
ID: {rule_id}
错误类别: {rule.get('error_category', '')}
错误原因: {rule.get('error_reason', '')}
IF 条件: {json.dumps(rule.get('if_conditions', []), ensure_ascii=False, indent=2)}
THEN 归类: {rule.get('then_category', '')}
置信度: {rule.get('confidence', 0)}

=== 支撑轨迹片段 ===
{traj_text}

=== 系统已有规则（用于判断冗余） ===
{json.dumps([{'id': r.get('id', ''), 'category': r.get('error_category', '')} for r in rules], ensure_ascii=False)}

=== 系统 Skill 清单及摘要 ===
Skill清单: {', '.join(skill_names)}
{skill_summaries}

请输出审查结果（严格 JSON 格式）。"""

        messages = [
            {"role": "system", "content": RATIONALITY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        parsed = None
        for attempt in range(1, max_retries + 1):
            raw = call_llm(messages)
            parsed = _parse_json(raw)
            if parsed and 'rationality_score' in parsed:
                break
            print(f"  ⚠ 规则 {rule_id} 合理性审查第 {attempt} 次解析失败")

        if parsed and 'rationality_score' in parsed:
            score = float(parsed['rationality_score'])
            issues = parsed.get('issues', [])
        else:
            # 降级：用原始 confidence 作为合理性的近似
            score = rule.get('confidence', 0.5) * 0.8
            issues = ["LLM审查解析失败，使用原始confidence估算"]

        scores[rule_id] = (score, issues)
        print(f"  规则 {rule_id} 合理性得分: {score:.2f}")

    return scores


def _parse_json(raw: str) -> Optional[Dict]:
    """从 LLM 回复中提取 JSON"""
    try:
        return json.loads(raw)
    except:
        pass

    code_blocks = re.findall(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if not code_blocks:
        code_blocks = re.findall(r'```\s*(.*?)\s*```', raw, re.DOTALL)
    for block in code_blocks:
        try:
            return json.loads(block.strip())
        except:
            pass

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except:
            pass

    return None


# ==================== 正确行为指引生成 ====================

BEHAVIOR_SYSTEM_PROMPT = """你是一个对话式 AI 系统的行为规范专家。

你将收到一条 badcase 规则（包含错误描述和触发条件），请基于这些信息，
生成一段"正确行为指引"——描述在对应场景下，agent 应该怎么做。

核心原则：
- 聚焦"最终状态"而非"具体步骤"：描述最终应达成的正确状态，而非列举执行步骤
- 从原则层面概括，不陷入中间细节
- 用"在...情况下，agent应确保..."的格式
- 一句话概括，不超过50字
- 不要引用规则文档，只凭对业务场景的常识判断

输出格式（严格一行文本，不要加前缀标签）：
在[触发场景]情况下，agent应确保[最终正确状态]"""


def generate_expected_behaviors(rules: List[Dict], max_retries: int = 2) -> Dict[str, str]:
    """为每条规则生成"正确行为指引"

    Returns:
        {rule_id: expected_behavior_text}
    """
    behaviors = {}
    for rule in rules:
        rule_id = rule.get('id', '')
        error_reason = rule.get('error_reason', '')
        then_category = rule.get('then_category', '')
        if_conditions = rule.get('if_conditions', [])

        # 从 if_conditions 提取场景描述
        scene_parts = []
        for cond in if_conditions:
            desc = cond.get('feature_description', '')
            if desc:
                scene_parts.append(desc)

        scene_text = '；'.join(scene_parts) if scene_parts else then_category

        prompt = f"""请为以下 badcase 规则生成"正确行为指引"：

=== 规则 ===
错误类别: {then_category}
错误原因: {error_reason}
触发场景: {scene_text}

请输出一句话正确行为指引（不超过50字）。"""

        messages = [
            {"role": "system", "content": BEHAVIOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        behavior = ""
        for attempt in range(max_retries):
            raw = call_llm(messages).strip()
            # 检测 LLM 调用失败（超时/错误等）
            if raw.startswith('[') and ('超时' in raw or '错误' in raw or '失败' in raw):
                continue
            # 清理可能的标签前缀
            raw = re.sub(r'^(正确行为指引|expected_behavior|行为指引)[：:]\s*', '', raw)
            raw = re.sub(r'^["「『]|["」』]$', '', raw)
            if raw and len(raw) <= 100:
                behavior = raw
                break
            elif raw:
                behavior = raw[:80]
                break

        if not behavior:
            # 降级：从 error_reason 反转生成
            behavior = f"在{then_category}场景下，agent应确保遵守相关合规要求"

        behaviors[rule_id] = behavior
        print(f"  规则 {rule_id} 正确行为指引: {behavior}")

    return behaviors


# ==================== 综合排序 ====================

def rank_rules(rules: List[Dict],
               frequency_scores: Dict[str, float],
               importance_scores: Dict[str, float],
               rationality_scores: Dict[str, Tuple[float, List[str]]],
               expected_behaviors: Dict[str, str],
               weights: Tuple[float, float, float] = (0.4, 0.3, 0.3)) -> List[RankedRule]:
    """对规则进行三维加权排序"""

    w1, w2, w3 = weights  # 合理性, 重要性, 频率

    ranked = []
    for rule in rules:
        rule_id = rule.get('id', '')
        r_score = rationality_scores.get(rule_id, (0.5, []))[0]
        i_score = importance_scores.get(rule_id, 0.5)
        f_score = frequency_scores.get(rule_id, 0.5)

        total = w1 * r_score + w2 * i_score + w3 * f_score

        ranked_rule = RankedRule(
            rule_id=rule_id,
            error_category=rule.get('error_category', ''),
            error_reason=rule.get('error_reason', ''),
            if_conditions=rule.get('if_conditions', []),
            then_category=rule.get('then_category', ''),
            skill_attribution=rule.get('skill_attribution', {}),
            supporting_trajectories=rule.get('supporting_trajectories', []),
            few_shots=rule.get('few_shots', {}),
            confidence=rule.get('confidence', 0.0),
            score_rationality=r_score,
            score_importance=i_score,
            score_frequency=f_score,
            score_total=total,
            expected_behavior=expected_behaviors.get(rule_id, ''),
        )
        ranked.append(ranked_rule)

    # 按综合得分降序
    ranked.sort(key=lambda r: r.score_total, reverse=True)

    # 赋排名
    for i, r in enumerate(ranked, 1):
        r.rank = i

    return ranked


def filter_rules(ranked: List[RankedRule],
                 top_k: Optional[int] = None,
                 min_score: float = 0.0,
                 drop_bottom: int = 0) -> List[RankedRule]:
    """按综合得分筛选规则"""

    for r in ranked:
        r.rejected = False

    # 先标记 drop_bottom
    if drop_bottom > 0 and drop_bottom < len(ranked):
        for r in ranked[-drop_bottom:]:
            r.rejected = True

    # 再标记 min_score 以下的
    if min_score > 0:
        for r in ranked:
            if r.score_total < min_score:
                r.rejected = True

    # 最后标记 top_k 之外的
    if top_k is not None and top_k > 0:
        for r in ranked[top_k:]:
            r.rejected = True

    return ranked


# ==================== 自然语言化输出 ====================

def format_rule_nl(rule: RankedRule, index: int) -> str:
    """将单条规则转化为自然语言描述

    格式参照人工归纳规则.txt：编号、判定标准、典型表现、责任归属
    """
    lines = []

    # 序号 + 类别名
    cn_num = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
    prefix = cn_num[index] if index < len(cn_num) else f"({index + 1})"
    lines.append(f"{prefix}{rule.then_category}：")

    # 判定标准（从 error_reason 生成）
    lines.append(f"\t判定标准：{rule.error_reason}")

    # 正确行为指引（v4新增）
    if rule.expected_behavior:
        lines.append(f"\t正确行为：{rule.expected_behavior}")

    # IF-THEN 自然语言化
    lines.append(f"\t检查条件：")
    for cond in rule.if_conditions:
        feat_desc = cond.get('feature_description', '')
        judgment = cond.get('judgment_criteria', '')
        feat_name = cond.get('feature', '')

        if feat_desc:
            # 用自然语言描述触发场景
            lines.append(f"\t\t当 {feat_desc}")
            if judgment:
                # 把判定标准转化为 "必须...否则判定为..." 格式
                # judgment 通常包含 True/False 两侧描述
                true_part = ""
                if "True：" in judgment or "True:" in judgment:
                    parts = re.split(r'True[：:]', judgment, maxsplit=1)
                    if len(parts) > 1:
                        true_part = parts[1].split('False')[0].strip().rstrip('。；;')
                        # 去掉尾部 "False" 残留
                        true_part = re.split(r'False[：:]', true_part)[0].strip()

                if true_part:
                    lines.append(f"\t\t→ agent 必须：{true_part}")
                    lines.append(f"\t\t→ 否则判定为「{rule.then_category}」")
                else:
                    lines.append(f"\t\t→ 判定标准：{judgment[:150]}")
                    lines.append(f"\t\t→ 否则判定为「{rule.then_category}」")
        else:
            # 兜底：用 checkpoint ID
            lines.append(f"\t\t触发特征：{feat_name} {cond.get('op', '==')} {cond.get('value', 0)}")
            lines.append(f"\t\t→ 否则判定为「{rule.then_category}」")

    # 典型表现（从 few_shots 提炼）
    neg_examples = rule.few_shots.get('negative_examples', [])
    if neg_examples:
        lines.append(f"\t典型表现：")
        for ex in neg_examples[:2]:
            why = ex.get('why', '')
            if why:
                lines.append(f"\t\t○ {why}")
            else:
                turns = ex.get('turns', [])
                if turns:
                    # 摘录前2轮对话关键信息
                    for turn in turns[:2]:
                        lines.append(f"\t\t○ {turn[:100]}")

    # 责任归属
    top3 = rule.skill_attribution.get('top3', [])
    if top3:
        lines.append(f"\t责任归属：")
        for i, item in enumerate(top3):
            if not isinstance(item, dict):
                continue
            sname = item.get('skill_name', '')
            prule = item.get('problematic_rule', '')
            conf = item.get('confidence', 0.0)
            label = "主要" if i == 0 else "次要"
            if sname:
                lines.append(f"\t\t- {label}：{sname} — {prule}（置信度 {conf:.0%}）")
            else:
                lines.append(f"\t\t- {label}：{prule}（置信度 {conf:.0%}）")

    # 排名信息
    lines.append(f"\t[排名 #{rule.rank} | 综合{rule.score_total:.2f} = 合理性{rule.score_rationality:.2f} × 0.4 + 重要性{rule.score_importance:.2f} × 0.3 + 频率{rule.score_frequency:.2f} × 0.3]")

    if rule.rejected:
        lines.append(f"\t⚠ 此规则已被筛选剔除")

    lines.append("")  # 空行分隔
    return "\n".join(lines)


def generate_natural_language_text(ranked: List[RankedRule], output_path: str):
    """生成完整的自然语言规则文本"""
    lines = []

    # 头部说明
    accepted = [r for r in ranked if not r.rejected]
    rejected = [r for r in ranked if r.rejected]

    lines.append("是 agent 是否产生了 badcase 的评判者，根据以下类别进行评判，并界定问题出在哪里。")
    lines.append("（按合理性、重要性、出现频率三维加权排序，从高到低）")
    lines.append(f"（评分权重：合理性×0.4 + 重要性×0.3 + 频率×0.3）")
    lines.append("")
    lines.append(f'符合如下类别的属于"失败/部分通过"，共 {len(accepted)} 条有效规则：')
    lines.append("")

    # 逐条输出（先输出保留的）
    index = 0
    for r in ranked:
        if not r.rejected:
            lines.append(format_rule_nl(r, index))
            index += 1

    # 被剔除的规则单独列出
    if rejected:
        lines.append("")
        lines.append("=" * 60)
        lines.append(f"以下 {len(rejected)} 条规则已被筛选剔除（得分过低），仅供参考：")
        lines.append("=" * 60)
        lines.append("")
        for r in rejected:
            lines.append(format_rule_nl(r, index))
            index += 1

    text = "\n".join(lines)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(text)

    return text


def save_ranked_json(ranked: List[RankedRule], output_path: str):
    """保存排序后的规则为 JSON"""
    data = {
        "meta": {
            "total_rules": len(ranked),
            "accepted_rules": sum(1 for r in ranked if not r.rejected),
            "rejected_rules": sum(1 for r in ranked if r.rejected),
        },
        "rules": [],
        "rejected_rules": []
    }

    for r in ranked:
        rule_data = {
            'id': r.rule_id,
            'error_category': r.error_category,
            'error_reason': r.error_reason,
            'if_conditions': r.if_conditions,
            'then_category': r.then_category,
            'skill_attribution': r.skill_attribution,
            'supporting_trajectories': r.supporting_trajectories,
            'few_shots': r.few_shots,
            'confidence': r.confidence,
            # Phase6 排序字段
            'expected_behavior': r.expected_behavior,
            'score_rationality': round(r.score_rationality, 4),
            'score_importance': round(r.score_importance, 4),
            'score_frequency': round(r.score_frequency, 4),
            'score_total': round(r.score_total, 4),
            'rank': r.rank,
            'rejected': r.rejected,
        }
        if r.rejected:
            data['rejected_rules'].append(rule_data)
        else:
            data['rules'].append(rule_data)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return data


# ==================== 打印报告 ====================

def print_report(ranked: List[RankedRule]):
    """打印排序筛选报告"""
    accepted = [r for r in ranked if not r.rejected]
    rejected = [r for r in ranked if r.rejected]

    print("\n" + "=" * 70)
    print("【Phase 6 规则排序筛选报告】")
    print("=" * 70)

    print(f"\n共 {len(ranked)} 条规则，保留 {len(accepted)} 条，剔除 {len(rejected)} 条\n")

    print(f"{'排名':<6}{'规则ID':<8}{'错误类别':<30}{'综合':<8}{'合理性':<8}{'重要性':<8}{'频率':<8}{'状态'}")
    print("-" * 100)

    for r in ranked:
        status = "保留" if not r.rejected else "剔除"
        print(f"#{r.rank:<5}{r.rule_id:<8}{r.error_category[:28]:<30}{r.score_total:<8.3f}"
              f"{r.score_rationality:<8.3f}{r.score_importance:<8.3f}{r.score_frequency:<8.3f}{status}")

    if rejected:
        print(f"\n被剔除的规则：")
        for r in rejected:
            print(f"  {r.rule_id} ({r.error_category}) — 综合{r.score_total:.3f}")

    print("\n" + "=" * 70)


# ==================== 主流程 ====================

def main():
    parser = argparse.ArgumentParser(description="阶段6：规则语言化 + 排序筛选")
    parser.add_argument("--rules", type=str, required=True,
                        help="rules.json 文件路径（Phase5 输出）")
    parser.add_argument("--categories", type=str, required=True,
                        help="phase3output.json 文件路径")
    parser.add_argument("--trajectories", type=str, required=True,
                        help="原始轨迹数据文件或目录")
    parser.add_argument("--skills", type=str, required=True,
                        help="skill markdown 文件或目录")
    parser.add_argument("--output", type=str, default="rules_natural_language.txt",
                        help="自然语言规则输出文件 (.txt)")
    parser.add_argument("--output-json", type=str, default="rules_ranked.json",
                        help="排序后规则输出文件 (.json)")
    parser.add_argument("--top-k-rules", type=int, default=None,
                        help="保留前 K 条规则（默认 None=全部保留）")
    parser.add_argument("--min-score", type=float, default=0.0,
                        help="最低综合得分线（默认 0.0=不限制）")
    parser.add_argument("--drop-bottom", type=int, default=0,
                        help="去掉排名垫底的 N 条（默认 0）")
    parser.add_argument("--weights", type=str, default="0.4,0.3,0.3",
                        help="排序权重 w1,w2,w3（合理性,重要性,频率，默认 0.4,0.3,0.3）")
    parser.add_argument("--no-llm-review", action="store_true",
                        help="跳过 LLM 合理性审查（仅用启发式计算）")
    parser.add_argument("--max-retries", type=int, default=2,
                        help="LLM 合理性审查每条规则最大重试次数（默认 2）")
    args = parser.parse_args()

    # 解析权重
    weights = tuple(float(w) for w in args.weights.split(','))
    assert len(weights) == 3, "权重必须为3个数值，用逗号分隔"
    assert abs(sum(weights) - 1.0) < 0.01, "权重之和应约为1.0"

    # 加载数据
    print("加载数据...")
    rules = load_rules(args.rules)
    categories_data = load_categories(args.categories)
    trajectories = load_trajectories(args.trajectories)
    skills = load_skills(args.skills)

    print(f"  规则: {len(rules)} 条")
    print(f"  类别: {len(categories_data.get('categories', []))} 个")
    print(f"  轨迹: {len(trajectories)} 条")
    print(f"  Skill: {len(skills)} 个")
    for s in skills:
        print(f"    - {s['name']}")

    # 1. 计算频率得分
    print("\n计算频率得分...")
    frequency_scores = compute_frequency_scores(rules, len(trajectories))

    # 2. 计算重要性得分
    print("计算重要性得分...")
    importance_scores = compute_importance_scores(rules, categories_data)

    # 2.5 生成正确行为指引
    print("\n生成正确行为指引...")
    expected_behaviors = generate_expected_behaviors(rules, max_retries=args.max_retries)

    # 3. 计算合理性得分（LLM 对抗性审查）
    if args.no_llm_review:
        print("\n跳过 LLM 合理性审查（--no-llm-review）")
        rationality_scores = {}
        for rule in rules:
            rule_id = rule.get('id', '')
            # 不做 LLM 审查时，用 confidence 近似
            rationality_scores[rule_id] = (rule.get('confidence', 0.5), [])
    else:
        print("\n计算合理性得分（LLM 对抗性审查）...")
        rationality_scores = compute_rationality_scores(
            rules, trajectories, skills, max_retries=args.max_retries
        )

    # 4. 综合排序
    print("\n综合排序...")
    ranked = rank_rules(rules, frequency_scores, importance_scores,
                        rationality_scores, expected_behaviors, weights=weights)

    # 5. 筛选
    print("筛选规则...")
    ranked = filter_rules(ranked,
                          top_k=args.top_k_rules,
                          min_score=args.min_score,
                          drop_bottom=args.drop_bottom)

    # 6. 生成自然语言文本
    print(f"\n生成自然语言规则文本...")
    text = generate_natural_language_text(ranked, args.output)
    print(f"  已保存: {args.output} ({len(text)} 字符)")

    # 7. 保存排序 JSON
    print(f"保存排序结果...")
    save_ranked_json(ranked, args.output_json)
    print(f"  已保存: {args.output_json}")

    # 8. 打印报告
    print_report(ranked)

    accepted = sum(1 for r in ranked if not r.rejected)
    rejected = sum(1 for r in ranked if r.rejected)
    print(f"\n提示:")
    print(f"  1. 自然语言规则文件 {args.output} 可作为评估器（checker）的提示词输入")
    print(f"  2. 排序详情见 {args.output_json}，包含各维度得分")
    print(f"  3. 被剔除的 {rejected} 条规则仍保留在输出文件中，标注为'剔除'，方便回溯")
    print(f"  4. 如需调整筛选标准，修改 --top-k-rules / --min-score / --drop-bottom 参数")


if __name__ == "__main__":
    main()
