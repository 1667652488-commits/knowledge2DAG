#!/usr/bin/env python3
"""
阶段5：冷数据规则挖掘 —— 从特征矩阵 + 类别定义 + Skill 提示词，输出可部署规则

输出格式：
  {
    "id": "R001",
    "error_category": "大类-小类",
    "error_reason": "描述为什么会产生这个错误",
    "if_conditions": [{"feature": "xxx", "op": "==", "value": 0}],
    "then_category": "错误类别名称",
    "skill_attribution": {
      "top3": [...],
      "note": "..."
    },
    "few_shots": {
      "positive_examples": [
        {
          "conversation_id": "conv-xxx",
          "turns": ["第1轮[用户]: ...", "第2轮[Agent]: ..."],
          "why": "这是一个正例：agent正确处理了该场景"
        }
      ],
      "negative_examples": [
        {
          "conversation_id": "conv-xxx",
          "turns": ["第1轮[用户]: ...", "第2轮[Agent]: ..."],
          "why": "这是一个负例：agent违反了规则，导致badcase"
        }
      ]
    }
  }

输入：
  - phase4 features.jsonl（特征矩阵）
  - phase3 categories.json（类别 + checkpoint 定义）
  - trajectories.jsonl（原始轨迹，用于 LLM 分析上下文）
  - skill markdown 文件或目录（fund_planning_skill.md, product_select_skill.md ...）

用法：
  python phase5_rule_mining.py \
    --features features.jsonl \
    --categories phase3_categories.json \
    --trajectories trajectories.jsonl \
    --skills skills/ \
    --output rules.json \
    --top-k-checkpoints 3
"""

import json
import hashlib
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
class SkillInfo:
    name: str  # skill 文件名或内部标识
    raw_content: str  # 原始 markdown 内容
    summary: str = ""  # 核心 prompt 摘要（后续由 LLM 或启发式提取）


@dataclass
class CheckpointDef:
    checkpoint_id: str
    category_id: str
    category_name: str
    description: str
    judgment_criteria: str


@dataclass
class Rule:
    rule_id: str
    error_category: str  # 大类-小类
    error_reason: str
    if_conditions: List[Dict[str, Any]]
    then_category: str
    skill_attribution: Dict[str, Any]
    supporting_trajectories: List[str]  # 支撑这条规则的轨迹ID
    few_shots: Dict[str, List[Dict]]  # {positive_examples: [...], negative_examples: [...]}
    confidence: float  # 规则整体置信度


# ==================== Skill 加载与摘要 ====================

def load_skills(path: str) -> List[SkillInfo]:
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
        # 取前 3000 字符作为摘要（多数 prompt 在前半部分）
        summary = content[:3000]
        skills.append(SkillInfo(
            name=f.stem,
            raw_content=content,
            summary=summary
        ))

    return skills


def extract_skill_system_prompt(skill: SkillInfo) -> str:
    """从 skill markdown 中提取 system prompt 部分"""
    content = skill.raw_content
    # 找 system prompt 标记
    patterns = [
        r'#+\s*System\s*Prompt\s*\n(.*?)(?=\n#+\s|\Z)',
        r'##\s*system\s*\n(.*?)(?=\n##\s|\Z)',
        r'```system\s*\n(.*?)```',
        r'```\s*\n(.*?)```',  #  fallback
    ]
    for pat in patterns:
        match = re.search(pat, content, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()[:2000]
    # fallback: 取开头
    return content[:2000]


# ==================== 特征与类别加载 ====================

def load_features(path: str) -> List[Dict]:
    """加载 phase4 特征输出（JSONL）"""
    features = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                features.append(json.loads(line))
    return features


def load_categories(path: str) -> List[CheckpointDef]:
    """从 phase3 解析 checkpoint → 类别映射"""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    phase3 = data.get('phase3_output', data)
    checkpoints = []

    for cat in phase3.get('categories', []):
        cat_id = cat.get('category_id', '')
        cat_name = cat.get('category_name', '')
        for chk in cat.get('binary_checkpoints', []):
            checkpoints.append(CheckpointDef(
                checkpoint_id=chk.get('checkpoint_id', ''),
                category_id=cat_id,
                category_name=cat_name,
                description=chk.get('description', ''),
                judgment_criteria=chk.get('judgment_criteria', '')
            ))

    return checkpoints


def build_checkpoint_to_category(checkpoints: List[CheckpointDef]) -> Dict[str, Tuple[str, str]]:
    """checkpoint_id → (category_id, category_name)"""
    mapping = {}
    for cp in checkpoints:
        mapping[cp.checkpoint_id] = (cp.category_id, cp.category_name)
    return mapping


# ==================== 特征矩阵分析 ====================

def analyze_feature_patterns(features: List[Dict],
                             cp_to_cat: Dict[str, Tuple[str, str]],
                             top_k: int = 3) -> Dict[str, List[Dict]]:
    """
    按类别聚合特征矩阵，找出每个类别最相关的 checkpoints。
    返回：category_id → [{checkpoint_id, fail_count, fail_rate, total}, ...]
    """
    # 先收集所有 checkpoint IDs（以 _final 结尾的键）
    all_cp_ids = set()
    for feat in features:
        for key in feat.keys():
            if key.endswith('_final') and not key.startswith('script_id'):
                cp_id = key.replace('_final', '')
                all_cp_ids.add(cp_id)

    # 按类别分组 checkpoint
    cat_to_cps = defaultdict(list)
    for cp_id, (cat_id, cat_name) in cp_to_cat.items():
        cat_to_cps[cat_id].append(cp_id)

    # 统计每个类别下各 checkpoint 的违反情况
    cat_patterns = {}
    for cat_id, cp_ids in cat_to_cps.items():
        stats = []
        for cp_id in cp_ids:
            final_key = f'{cp_id}_final'
            fail_count = sum(1 for f in features if f.get(final_key) == 0)
            total = sum(1 for f in features if f.get(final_key) in [0, 1])  # 排除 NA
            if total > 0:
                stats.append({
                    'checkpoint_id': cp_id,
                    'fail_count': fail_count,
                    'total': total,
                    'fail_rate': fail_count / total,
                })

        # 按 fail_count 排序，取 top_k
        stats.sort(key=lambda x: x['fail_count'], reverse=True)
        cat_patterns[cat_id] = stats[:top_k]

    return cat_patterns


def extract_category_examples(features: List[Dict],
                               checkpoints: List[CheckpointDef],
                               cat_id: str,
                               trajectories: List[Dict],
                               max_examples: int = 5) -> List[Dict]:
    """
    提取某类别下的典型轨迹样本（用于 LLM 分析上下文）。
    选择该类别下 checkpoint 违反最多的轨迹。
    """
    cp_ids = [cp.checkpoint_id for cp in checkpoints if cp.category_id == cat_id]

    # 计算每条轨迹在该类别下的违反分数
    scored = []
    for feat in features:
        score = 0
        for cp_id in cp_ids:
            final_key = f'{cp_id}_final'
            if feat.get(final_key) == 0:
                score += 1
        if score > 0:
            scored.append((feat['conversation_id'], score))

    # 按违反分数排序
    scored.sort(key=lambda x: x[1], reverse=True)
    top_ids = [x[0] for x in scored[:max_examples]]

    # 找对应的轨迹
    examples = []
    traj_map = {t.get('conversation_id', ''): t for t in trajectories}
    for conv_id in top_ids:
        traj = traj_map.get(conv_id)
        if traj:
            # 精简：只保留最近 4 轮
            history = traj.get('messages', [])[-4:]
            examples.append({
                'conversation_id': conv_id,
                'history_snippet': history
            })

    return examples


# ==================== LLM 规则归纳 + Skill 归因 ====================

RULE_MINING_SYSTEM_PROMPT = """你是一个对话式 AI 系统的质量分析与归因专家。

你的任务是根据以下输入，归纳出一条可部署的 badcase 规则，并定位到最相关的 Skill（含系统提示词）。

输入包含：
1. 某错误类别的定义和典型检查点
2. 该类别下触发违反的典型轨迹样本
3. 系统中所有 Skill 的核心提示词摘要

输出要求（严格 JSON）：
{
  "error_reason": "用一段话描述：为什么 agent 会在这个类别下犯错（从对话行为角度分析，不要猜测内部实现）",
  "if_conditions": [
    {"feature": "checkpoint_final字段名（如CHK001_final）", "op": "==", "value": 0}
  ],
  "skill_attribution": {
    "top3": [
      {"skill_name": "skill文件名（必须从提供的Skill清单中选择）", "problematic_rule": "该skill中导致此问题的具体规则或缺失约束（不超过30字）", "confidence": 0.0-1.0},
      {"skill_name": "...", "problematic_rule": "...", "confidence": 0.0-1.0},
      {"skill_name": "...", "problematic_rule": "...", "confidence": 0.0-1.0}
    ],
    "note": "如果最高置信度 < 0.5，则写'缺乏明确的定位规则，需要LLM根据具体语义分析'，否则留空"
  },
  "confidence": 0.0-1.0,
  "few_shots": {
    "positive_examples": [
      {
        "conversation_id": "conv-xxx",
        "turns": ["对话轮次文本（用户输入和agent回复）"],
        "why": "为什么这是正例：agent正确处理了场景"
      }
    ],
    "negative_examples": [
      {
        "conversation_id": "conv-xxx",
        "turns": ["对话轮次文本（用户输入和agent回复）"],
        "why": "为什么这是负例：agent违反了规则"
      }
    ]
  }
}

Skill 归因原则：
- skill_name 必须从输入中提供的【系统 Skill 清单】选择文件名，不能自创名称
- 如果根因在系统全局层面（非任一具体skill），则 skill_name 填 "AgentRule"，并在 problematic_rule 中说明是哪条全局规则缺失
- problematic_rule 要具体指出该 skill prompt 中哪个环节/哪条规则导致了问题或缺少约束
- 不要把错误归因给 "所有 skill"，要找到最相关的 1-3 个
- confidence 反映你对归因的信心：高=明确看到 skill prompt 缺少某约束条款；中=该 skill 可能有关系但不确定具体哪条；低=只是猜测
- 如果无法从 skill prompt 中找到直接关联，则最高 confidence 设为 0.3 以下，并标注 note

few_shots 选取原则：
- positive_examples：从提供的轨迹中找 agent 正确处理该场景的 1-2 个例子（如果全量都是badcase则可能没有）
- negative_examples：从提供的轨迹中找 1-2 个最能体现该规则触发条件的 badcase 典型例子
- 直接摘录原始轨迹中的对话文本（用户输入 + agent回复），不要改写
- why 字段解释这个例子为什么支持该规则（正例说明什么行为是对的，负例说明什么行为触发了错误）

规则粒度原则：
- 如果同一类别下存在明显不同的触发模式或错误子类型，应输出多条规则（每条规则有独立的 if_conditions、error_reason、skill_attribution）
- 此时输出格式为 JSON 数组，每个元素是一条规则的完整 JSON 对象
- 只有当类别下的错误模式高度一致时，才输出单条规则（即单个 JSON 对象，不包在数组中）"""


def build_rule_mining_prompt(category_name: str,
                              category_desc: str,
                              checkpoint_stats: List[Dict],
                              example_trajectories: List[Dict],
                              skills: List[SkillInfo]) -> str:
    """构建规则挖掘 prompt"""

    # 类别信息
    lines = [
        f"【错误类别】{category_name}",
        f"【类别定义】{category_desc}",
        "",
        "【相关检查点统计】",
    ]
    for stat in checkpoint_stats:
        lines.append(f"  - {stat['checkpoint_id']}: {stat['fail_count']}/{stat['total']} 条轨迹违反 (fail_rate={stat['fail_rate']:.1%})")

    # 典型轨迹
    lines.extend(["", "【典型违反轨迹样本（最近4轮）】"])
    for ex in example_trajectories:
        lines.append(f"\n轨迹ID: {ex['conversation_id']}")
        for turn in ex.get('history_snippet', []):
            role = turn.get('role', '?')
            content = turn.get('content', '')[:200]
            lines.append(f"  [{role}]: {content}")

    # Skill 清单 + 摘要
    skill_names = [skill.name for skill in skills]
    lines.extend(["", f"【系统 Skill 清单（归因时 skill_name 只能从以下选择）】"])
    lines.append("  " + " / ".join(skill_names))

    lines.extend(["", "【系统 Skill 提示词摘要】"])
    for skill in skills:
        prompt_summary = extract_skill_system_prompt(skill)
        lines.append(f"\nSkill: {skill.name}")
        lines.append(f"核心提示词摘要:\n{prompt_summary}")

    lines.extend(["", "【任务】"])
    lines.append("请基于以上信息，归纳出该错误类别的规则：")
    lines.append("1. 错误原因：为什么 agent 会在该类别下犯错（从对话行为描述，不猜测内部实现）")
    lines.append("2. IF-THEN 规则：哪些 checkpoint_final=0 的组合触发这个类别")
    lines.append("3. Skill 归因：列出 top3 最相关的 skill / 提示词约束，附置信度")
    lines.append("请只输出 JSON，不要其他文字。")

    return "\n".join(lines)


def parse_rule_json(raw: str) -> Optional[Dict]:
    """解析 LLM 返回的规则 JSON

    支持两种格式：
    - 单条规则：直接返回 dict
    - 多条规则（数组）：返回 {"_multi_rules": [...]} 以区分
    """
    # 尝试解析为 JSON 数组（多条规则）
    def _try_parse(text):
        parsed = json.loads(text)
        if isinstance(parsed, list):
            # 多条规则
            return {"_multi_rules": parsed}
        return parsed

    try:
        return _try_parse(raw)
    except:
        pass

    code_blocks = re.findall(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if not code_blocks:
        code_blocks = re.findall(r'```\s*(.*?)\s*```', raw, re.DOTALL)
    for block in code_blocks:
        try:
            return _try_parse(block.strip())
        except:
            pass

    # 尝试匹配 [...] 数组
    match_arr = re.search(r'\[.*\]', raw, re.DOTALL)
    if match_arr:
        try:
            return _try_parse(match_arr.group())
        except:
            pass

    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            return _try_parse(match.group())
        except:
            pass

    return None


# ==================== 降级兜底策略 ====================

FALLBACK_RULE_PREFIX = "[降级规则-LLM解析失败]"


def build_fallback_rule(cat_id: str, cat_name: str, cat_desc: str,
                         checkpoint_stats: List[Dict],
                         example_trajectories: List[Dict]) -> Dict:
    """降级策略：当 LLM 多次解析失败时，用已有结构化数据直接构造规则

    保留最核心的 if_conditions 和支撑轨迹，确保类别不会丢失。
    error_reason / skill_attribution / few_shots 为简化版本，需人工审核补充。
    """
    # if_conditions：直接基于 checkpoint_stats 构造
    if_conditions = [
        {"feature": f"{stat['checkpoint_id']}_final", "op": "==", "value": 0}
        for stat in checkpoint_stats
    ]

    # error_reason：用类别描述 + 检查点统计拼接
    cp_summaries = "; ".join(
        f"{s['checkpoint_id']}违反率{s['fail_rate']:.0%}({s['fail_count']}/{s['total']}条)"
        for s in checkpoint_stats
    )
    error_reason = (
        f"{FALLBACK_RULE_PREFIX} {cat_desc} "
        f"（检查点统计：{cp_summaries}）"
    )

    # skill_attribution：空，标注需要人工审核
    skill_attribution = {
        "top3": [],
        "note": "LLM多次解析失败，skill归因缺失，需人工审核补充（需定位到具体skill文件名）"
    }

    # few_shots：从典型轨迹中提取最近2轮对话构造负例
    negative_examples = []
    for ex in example_trajectories[:2]:
        turns = []
        for turn in ex.get('history_snippet', [])[-2:]:
            role = turn.get('role', '?')
            content = turn.get('content', '')[:200]
            turns.append(f"[{role}]: {content}")
        if turns:
            negative_examples.append({
                "conversation_id": ex.get('conversation_id', ''),
                "turns": turns,
                "why": "[降级提取] 该轨迹违反了此类别下的检查点"
            })

    return {
        "error_reason": error_reason,
        "if_conditions": if_conditions,
        "skill_attribution": skill_attribution,
        "confidence": 0.5,  # 降级规则固定中等置信度
        "few_shots": {
            "positive_examples": [],
            "negative_examples": negative_examples
        }
    }


# ==================== LLM 调用缓存 ====================

class RuleMiningCache:
    """规则挖掘结果缓存 —— 支持 cat_id + 类别定义 + 轨迹样本变更后自动失效"""

    def __init__(self, cache_dir: str = "phase5_rule_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.mem_cache: Dict[str, Dict] = {}

    def _make_key(self, cat_id: str, cat_desc: str,
                  checkpoint_stats: List[Dict],
                  example_conv_ids: List[str]) -> str:
        """缓存 key 纳入类别定义和输入数据，定义变更后自动失效"""
        raw = json.dumps({
            'cat_id': cat_id,
            'cat_desc': cat_desc,
            'stats': checkpoint_stats,
            'example_ids': sorted(example_conv_ids),
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, cat_id: str, cat_desc: str,
            checkpoint_stats: List[Dict],
            example_conv_ids: List[str]) -> Optional[Dict]:
        key = self._make_key(cat_id, cat_desc, checkpoint_stats, example_conv_ids)
        # 内存缓存
        if key in self.mem_cache:
            return self.mem_cache[key]
        # 磁盘缓存
        cache_file = self.cache_dir / f"{cat_id}_{key[:8]}.json"
        if cache_file.exists():
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.mem_cache[key] = data
                return data
            except:
                pass
        return None

    def set(self, cat_id: str, cat_desc: str,
            checkpoint_stats: List[Dict],
            example_conv_ids: List[str],
            parsed: Dict):
        key = self._make_key(cat_id, cat_desc, checkpoint_stats, example_conv_ids)
        self.mem_cache[key] = parsed
        # 写入磁盘
        cache_file = self.cache_dir / f"{cat_id}_{key[:8]}.json"
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(parsed, f, ensure_ascii=False, indent=2)


def mine_rule_for_category(cat_id: str,
                            cat_name: str,
                            cat_desc: str,
                            checkpoint_stats: List[Dict],
                            example_trajectories: List[Dict],
                            skills: List[SkillInfo],
                            rule_counter: int,
                            max_retries: int = 3,
                            cache: Optional[RuleMiningCache] = None,
                            checkpoints: Optional[List[CheckpointDef]] = None) -> List[Rule]:
    """用 LLM 对单个类别挖掘规则 + skill 归因（含重试与降级兜底）

    支持同一类别下拆分出多条规则（当 LLM 返回数组时）。
    返回规则列表（至少 1 条，降级时也为 1 条）。
    """

    # 构建 checkpoint_id → 描述信息的映射
    cp_info = {}
    if checkpoints:
        for cp in checkpoints:
            cp_info[cp.checkpoint_id] = {
                'description': cp.description,
                'judgment_criteria': cp.judgment_criteria,
            }

    example_conv_ids = [ex['conversation_id'] for ex in example_trajectories]

    # 查缓存
    if cache:
        cached = cache.get(cat_id, cat_desc, checkpoint_stats, example_conv_ids)
        if cached is not None:
            print(f"  类别 {cat_id} 命中缓存，跳过 LLM 调用")
            parsed = cached
        else:
            parsed = None
    else:
        parsed = None

    if parsed is None:
        prompt = build_rule_mining_prompt(cat_name, cat_desc, checkpoint_stats,
                                           example_trajectories, skills)

        messages = [
            {"role": "system", "content": RULE_MINING_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        print(f"  挖掘类别 {cat_id} 的规则...")
        last_raw = ""
        for attempt in range(1, max_retries + 1):
            raw = call_llm(messages)
            last_raw = raw
            parsed = parse_rule_json(raw)
            if parsed:
                if attempt > 1:
                    print(f"  ✓ 第 {attempt} 次重试成功")
                break
            print(f"  ⚠ 类别 {cat_id} 第 {attempt}/{max_retries} 次解析失败: {raw[:100]}...")

        if not parsed:
            print(f"  ✗ 类别 {cat_id} {max_retries} 次重试均失败，启用降级策略")
            parsed = build_fallback_rule(cat_id, cat_name, cat_desc,
                                         checkpoint_stats, example_trajectories)

        # 写缓存（含降级结果，避免重复降级）
        if cache:
            cache.set(cat_id, cat_desc, checkpoint_stats, example_conv_ids, parsed)

    # parsed 此时一定不为 None（LLM成功/缓存命中/降级兜底）

    # 处理多规则的情况：LLM 可能返回数组格式 {"_multi_rules": [...]}
    rule_dicts = []
    if isinstance(parsed, dict) and '_multi_rules' in parsed:
        rule_dicts = parsed['_multi_rules']
        if not isinstance(rule_dicts, list):
            rule_dicts = [parsed]  # 兜底
    elif isinstance(parsed, list):
        rule_dicts = parsed
    else:
        rule_dicts = [parsed]

    # 逐条构造 Rule
    rules = []
    supporting = [ex['conversation_id'] for ex in example_trajectories]

    # 构建 skill 白名单（用于校验）
    valid_skill_names = set()
    if skills:
        valid_skill_names = {s.name for s in skills}

    for idx, single_parsed in enumerate(rule_dicts):
        if not isinstance(single_parsed, dict):
            continue

        # 构造 if_conditions（补充 checkpoint 描述，使下游可理解特征含义）
        if_conditions = single_parsed.get('if_conditions', [])
        if not if_conditions and checkpoint_stats:
            # 兜底：用 checkpoint_stats 构造
            if_conditions = [
                {"feature": f"{stat['checkpoint_id']}_final", "op": "==", "value": 0}
                for stat in checkpoint_stats
            ]
        # 无论来自 LLM 还是兜底，都补上 checkpoint 描述和判定标准
        for cond in if_conditions:
            feat_name = cond.get('feature', '')
            # 从 "CHK002_final" 中提取 checkpoint_id
            cp_id = feat_name.replace('_final', '') if feat_name.endswith('_final') else feat_name
            info = cp_info.get(cp_id, {})
            cond['feature_description'] = info.get('description', '')
            cond['judgment_criteria'] = info.get('judgment_criteria', '')

        # 构造 skill_attribution
        top3_raw = single_parsed.get('skill_attribution', {}).get('top3', [])
        note = single_parsed.get('skill_attribution', {}).get('note', '')

        # 规范化 top3：兼容新旧格式
        top3 = []
        for item in top3_raw[:3]:
            if not isinstance(item, dict):
                continue

            # 新格式：skill_name + problematic_rule
            skill_name = str(item.get('skill_name', ''))
            problematic_rule = str(item.get('problematic_rule', ''))

            # 兼容旧格式：skill_name_or_prompt
            if not skill_name and item.get('skill_name_or_prompt'):
                old_val = str(item['skill_name_or_prompt'])
                # 尝试匹配已知 skill 名
                matched = False
                for sname in valid_skill_names:
                    if sname.lower() in old_val.lower():
                        skill_name = sname
                        problematic_rule = old_val
                        matched = True
                        break
                if not matched:
                    # 旧格式无法识别为具体 skill，放到 problematic_rule
                    problematic_rule = old_val
                    skill_name = ''

            # 校验 skill_name 是否在白名单中
            if skill_name and valid_skill_names and skill_name not in valid_skill_names:
                # 尝试模糊匹配
                fuzzy_match = None
                for sname in valid_skill_names:
                    if skill_name.lower() in sname.lower() or sname.lower() in skill_name.lower():
                        fuzzy_match = sname
                        break
                if fuzzy_match:
                    skill_name = fuzzy_match
                else:
                    # 无法匹配，降级 confidence 并标注
                    if not note:
                        note = f"skill_name='{skill_name}'不在已知Skill清单中，需人工核实"
                    skill_name = ''
                    problematic_rule = f"[归因失败] {str(item.get('skill_name', ''))}: {problematic_rule}"

            top3.append({
                'skill_name': skill_name,
                'problematic_rule': problematic_rule,
                'confidence': float(item.get('confidence', 0.0))
            })

        skill_attribution = {
            'top3': top3,
            'note': note
        }

        # 整体置信度
        confidence = float(single_parsed.get('confidence', 0.5))

        # 构造 few_shots
        few_shots_raw = single_parsed.get('few_shots', {})
        positive_examples = []
        negative_examples = []

        for ex in few_shots_raw.get('positive_examples', [])[:2]:
            if isinstance(ex, dict):
                positive_examples.append({
                    'conversation_id': ex.get('conversation_id', ''),
                    'turns': ex.get('turns', []),
                    'why': ex.get('why', '')
                })

        for ex in few_shots_raw.get('negative_examples', [])[:2]:
            if isinstance(ex, dict):
                negative_examples.append({
                    'conversation_id': ex.get('conversation_id', ''),
                    'turns': ex.get('turns', []),
                    'why': ex.get('why', '')
                })

        few_shots = {
            'positive_examples': positive_examples,
            'negative_examples': negative_examples
        }

        # 多规则时，error_category 加子编号区分
        if len(rule_dicts) > 1:
            rule_id = f"R{rule_counter + idx:03d}"
            error_category = f"{cat_id}-{cat_name}"
            if single_parsed.get('error_reason', ''):
                # 取 error_reason 的前 10 字作为子类别标注
                sub_label = single_parsed.get('error_reason', '')[:10]
                then_category = f"{cat_name}（{sub_label}…）"
            else:
                then_category = cat_name
        else:
            rule_id = f"R{rule_counter:03d}"
            error_category = f"{cat_id}-{cat_name}"
            then_category = cat_name

        rule = Rule(
            rule_id=rule_id,
            error_category=error_category,
            error_reason=single_parsed.get('error_reason', ''),
            if_conditions=if_conditions,
            then_category=then_category,
            skill_attribution=skill_attribution,
            supporting_trajectories=supporting,
            few_shots=few_shots,
            confidence=confidence
        )
        rules.append(rule)

    if not rules:
        # 兜底：如果所有 rule_dicts 都不是 dict，至少构造一条降级规则
        fallback_parsed = build_fallback_rule(cat_id, cat_name, cat_desc,
                                              checkpoint_stats, example_trajectories)
        if_conditions = [
            {"feature": f"{stat['checkpoint_id']}_final", "op": "==", "value": 0}
            for stat in checkpoint_stats
        ]
        for cond in if_conditions:
            feat_name = cond.get('feature', '')
            cp_id = feat_name.replace('_final', '') if feat_name.endswith('_final') else feat_name
            info = cp_info.get(cp_id, {})
            cond['feature_description'] = info.get('description', '')
            cond['judgment_criteria'] = info.get('judgment_criteria', '')

        rule = Rule(
            rule_id=f"R{rule_counter:03d}",
            error_category=f"{cat_id}-{cat_name}",
            error_reason=fallback_parsed.get('error_reason', ''),
            if_conditions=if_conditions,
            then_category=cat_name,
            skill_attribution=fallback_parsed.get('skill_attribution', {'top3': [], 'note': ''}),
            supporting_trajectories=supporting,
            few_shots=fallback_parsed.get('few_shots', {'positive_examples': [], 'negative_examples': []}),
            confidence=0.5
        )
        rules.append(rule)

    return rules


# ==================== 主流程 ====================

def load_trajectories(path: str) -> List[Dict]:
    """加载原始轨迹，支持 .jsonl 文件、.json 文件、目录（批量读取 *.json）"""
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


def save_rules(rules: List[Rule], output_path: str, pretty: bool = True):
    """保存规则为 JSON"""
    data = []
    for rule in rules:
        data.append({
            'id': rule.rule_id,
            'error_category': rule.error_category,
            'error_reason': rule.error_reason,
            'if_conditions': rule.if_conditions,
            'then_category': rule.then_category,
            'skill_attribution': rule.skill_attribution,
            'supporting_trajectories': rule.supporting_trajectories,
            'few_shots': rule.few_shots,
            'confidence': rule.confidence,
        })

    with open(output_path, 'w', encoding='utf-8') as f:
        if pretty:
            json.dump(data, f, ensure_ascii=False, indent=2)
        else:
            json.dump(data, f, ensure_ascii=False)

    return data


def print_rules_report(rules: List[Rule]):
    print("\n" + "=" * 70)
    print("【Phase 5 规则挖掘报告】")
    print("=" * 70)
    print(f"\n共生成 {len(rules)} 条规则:\n")

    for rule in rules:
        print(f"  {rule.rule_id}: {rule.error_category}")
        if rule.error_reason.startswith(FALLBACK_RULE_PREFIX):
            print(f"    ⚠️ 降级规则（LLM解析失败，需人工审核）")
        print(f"    原因: {rule.error_reason[:80]}...")
        if_str = ' AND '.join(f"{c['feature']}{c['op']}{c['value']}" for c in rule.if_conditions)
        print(f"    IF: {if_str}")
        print(f"    THEN: {rule.then_category}")

        top3 = rule.skill_attribution.get('top3', [])
        note = rule.skill_attribution.get('note', '')
        if top3:
            print(f"    Skill归因:")
            for i, item in enumerate(top3, 1):
                sname = item.get('skill_name', '')
                prule = item.get('problematic_rule', '')[:40]
                conf = item.get('confidence', 0.0)
                label = f"{sname}" if sname else "[未定位]"
                print(f"      {i}. {label}: {prule}... (置信度 {conf:.0%})")
        if note:
            print(f"    注意: {note}")
        print(f"    支撑轨迹: {len(rule.supporting_trajectories)} 条")
        print(f"    规则置信度: {rule.confidence:.2f}")

        few_shots = rule.few_shots
        pos = few_shots.get('positive_examples', [])
        neg = few_shots.get('negative_examples', [])
        if pos:
            print(f"    正例({len(pos)}):")
            for ex in pos:
                cid = ex.get('conversation_id', '')
                print(f"      {cid}: {ex.get('why', '')[:60]}...")
        if neg:
            print(f"    负例({len(neg)}):")
            for ex in neg:
                cid = ex.get('conversation_id', '')
                print(f"      {cid}: {ex.get('why', '')[:60]}...")
        print()

    # 统计
    high_conf = sum(1 for r in rules if r.confidence >= 0.7)
    low_conf = sum(1 for r in rules if r.confidence < 0.5)
    fallback = sum(1 for r in rules if r.error_reason.startswith(FALLBACK_RULE_PREFIX))
    with_note = sum(1 for r in rules if r.skill_attribution.get('note', ''))

    print(f"高置信度规则(>=0.7): {high_conf}/{len(rules)}")
    print(f"低置信度规则(<0.5): {low_conf}/{len(rules)}")
    print(f"降级规则(LLM解析失败): {fallback}/{len(rules)}")
    print(f"需语义分析规则: {with_note}/{len(rules)}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="阶段5：冷数据规则挖掘")
    parser.add_argument("--features", type=str, required=True, help="phase4 特征文件 (.jsonl)")
    parser.add_argument("--categories", type=str, required=True, help="phase3 类别定义 (.json)")
    parser.add_argument("--trajectories", type=str, required=True, help="原始轨迹文件 (.jsonl/.json) 或目录")
    parser.add_argument("--skills", type=str, required=True, help="skill markdown 文件或目录")
    parser.add_argument("--output", type=str, default="rules.json", help="规则输出文件")
    parser.add_argument("--top-k-checkpoints", type=int, default=3,
                        help="每个类别取 top K 个 checkpoint 用于规则归纳")
    parser.add_argument("--max-examples", type=int, default=5,
                        help="每个类别取多少条典型轨迹样本给 LLM")
    parser.add_argument("--cat-ids", type=str, nargs='+', default=None,
                        help="只处理指定类别ID（调试用）")
    parser.add_argument("--cache-dir", type=str, default="phase5_rule_cache",
                        help="缓存目录（中断重启可复用，默认 phase5_rule_cache）")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="每个类别 LLM 解析失败时的最大重试次数（默认 3）")
    parser.add_argument("--max-rules", type=int, default=0,
                        help="最大规则数上限（0=不限制，按类别自然生成）")
    args = parser.parse_args()

    # 加载
    print("加载数据...")
    features = load_features(args.features)
    checkpoints = load_categories(args.categories)
    trajectories = load_trajectories(args.trajectories)
    skills = load_skills(args.skills)

    print(f"  特征轨迹: {len(features)} 条")
    print(f"  checkpoint定义: {len(checkpoints)} 个")
    print(f"  原始轨迹: {len(trajectories)} 条")
    print(f"  Skill文件: {len(skills)} 个")
    for s in skills:
        print(f"    - {s.name}")

    # 构建映射
    cp_to_cat = build_checkpoint_to_category(checkpoints)
    cat_to_name = {cp.category_id: cp.category_name for cp in checkpoints}
    cat_to_desc = {}
    with open(args.categories, 'r', encoding='utf-8') as f:
        cat_data = json.load(f)
    phase3 = cat_data.get('phase3_output', cat_data)
    for cat in phase3.get('categories', []):
        cat_to_desc[cat.get('category_id', '')] = cat.get('description', '')

    # 特征模式分析
    print("\n分析特征模式...")
    cat_patterns = analyze_feature_patterns(features, cp_to_cat, top_k=args.top_k_checkpoints)

    # 逐类别挖掘规则
    cache = RuleMiningCache(args.cache_dir)
    print(f"缓存目录: {args.cache_dir}")
    rules = []
    rule_counter = 1

    target_cats = args.cat_ids if args.cat_ids else list(cat_patterns.keys())

    print(f"\n开始规则挖掘（目标类别: {len(target_cats)} 个）...")
    print("=" * 60)

    for cat_id in target_cats:
        cat_name = cat_to_name.get(cat_id, cat_id)
        cat_desc = cat_to_desc.get(cat_id, '')
        cp_stats = cat_patterns.get(cat_id, [])

        print(f"\n类别 {cat_id} ({cat_name}):")
        if not cp_stats:
            print("  无违反数据，跳过")
            continue

        # 提取典型样本
        cat_cps = [cp for cp in checkpoints if cp.category_id == cat_id]
        examples = extract_category_examples(features, cat_cps, cat_id,
                                              trajectories, max_examples=args.max_examples)

        # 挖掘规则（支持同一类别下拆分多条规则）
        mined_rules = mine_rule_for_category(
            cat_id, cat_name, cat_desc,
            cp_stats, examples, skills,
            rule_counter,
            max_retries=args.max_retries,
            cache=cache,
            checkpoints=checkpoints
        )

        if mined_rules:
            # 重新编号 rule_id
            for rule in mined_rules:
                rule.rule_id = f"R{rule_counter:03d}"
                rules.append(rule)
                rule_counter += 1
                print(f"  ✓ 生成规则 {rule.rule_id} (置信度 {rule.confidence:.2f})")
        else:
            print(f"  ✗ 规则挖掘失败")

        # 检查最大规则数限制
        if args.max_rules > 0 and len(rules) >= args.max_rules:
            print(f"\n已达到最大规则数限制 ({args.max_rules})，停止挖掘")
            rules = rules[:args.max_rules]
            break

    # 保存
    print(f"\n保存 {len(rules)} 条规则到 {args.output}...")
    save_rules(rules, args.output)

    # 报告
    print_rules_report(rules)

    print(f"\n提示:")
    print("  1. 查看 rules.json 中的 if_conditions 是否准确反映特征组合")
    print("  2. 检查 skill_attribution.top3 的置信度分布——如果普遍 <0.5，说明 skill prompt 中缺少可定位的约束")
    print("  3. note='缺乏明确的定位规则' 的规则需要人工 review，或回到 phase3 细化 checkpoint 定义")
    print("  4. 高置信度规则(>=0.7)可直接用于运行时规则引擎")
    print("  5. 低置信度规则(<0.5)建议仅作为 LLM Fallback 的参考，不直接触发")


if __name__ == "__main__":
    main()
