#!/usr/bin/env python3
"""
阶段4 v3：LLM理解式特征提取 —— 配置驱动，零预设

核心设计：
- 每个 checkpoint 对每条轨迹，用 LLM 根据 checkpoint 定义做 0/1/NA 判定
- 轨迹结构完全配置化：字段名、角色名、内容字段全部可自定义
- 片段提取规则配置化："最后""用户输入"等规则映射到轨迹字段名
- 支持批量调用（一次请求判多个 checkpoints，降低成本）
- 支持缓存（相同 checkpoint + 轨迹不重复调用；checkpoint 定义变更后自动失效）

输入：
  - phase3_categories.json（binary_checkpoints 定义）
  - trajectories（.jsonl / .json / 目录，格式由 --trajectory-config 指定）
  - trajectory_config.json（可选：轨迹结构配置：字段名、角色映射、提取规则）

输出：
  - features.jsonl
  - feature_report.json
  - llm_judge_logs.jsonl（每次 LLM 判定的原始输入输出，用于审计）

参数说明：
  --trajectories       轨迹文件 (.jsonl/.json) 或目录（目录时扫描 *.json）
  --categories         phase3 输出的类别定义文件
  --trajectory-config  轨迹结构配置 JSON（默认兼容 customer/agent 格式）
  --output             特征输出文件（默认 features.jsonl）
  --report             统计报告文件（默认 feature_report.json）
  --log                LLM 判定审计日志（默认 llm_judge_logs.jsonl）
  --cache              缓存文件路径，中断重启可复用（默认 llm_judge_cache.json）
  --batch-size         批量模式每批检查点数（默认 5）
  --max-turns          每条轨迹提取的最大轮数（默认 10）
  --no-batch           禁用批量模式，改为逐个判定（更准但更慢更费 token）

用法：
  # 默认配置（兼容现有 customer/agent 格式，目录输入）
  python phase4_feature_extractor.py \
    --trajectories input_trace/0611v1/chosen/ --categories phase3output.json --output phase4output.json

  # 禁用批量模式（逐个判定，更精确）
  python phase4_feature_extractor.py \
    --trajectories input_trace/0611v1/chosen/ --categories phase3output.json --output phase4output.json --no-batch

  # 从 .jsonl 文件加载轨迹
  python phase4_feature_extractor.py \
    --trajectories data.jsonl --categories phase3output.json --output phase4output.json

  # 自定义轨迹格式（如 user/assistant 角色、messages 字段名等）
  python phase4_feature_extractor.py \
    --trajectories data.jsonl --categories phase3output.json \
    --trajectory-config my_format.json --output phase4output.json

  # 自定义轨迹配置示例（my_format.json）：
  # {
  #   "conversation_id_field": "conversation_id",
  #   "script_id_field": "script_id",
  #   "history_field": "history",
  #   "turn_field": "turn",
  #   "role_field": "role",
  #   "content_field": "content",
  #   "role_mapping": {
  #     "user": "user",
  #     "assistant": "assistant"
  #   },
  #   "evidence_rules": {
  #     "last": ["last", "final", "末尾", "结束"],
  #     "user_turns": ["用户输入", "用户", "user", "customer"],
  #     "agent_turns": ["agent回复", "agent", "assistant", "助手", "系统"]
  #   }
  # }
"""

import json
import os
import re
import hashlib
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from collections import Counter
from dataclasses import dataclass

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))
from common.llm_client import call_llm


# ==================== 数据模型 ====================

@dataclass
class CheckpointDef:
    checkpoint_id: str
    category_id: str
    description: str
    judgment_criteria: str
    evidence_location: str = ""


@dataclass
class TrajectoryConfig:
    """
    轨迹数据结构配置 —— 全部可自定义，零预设。
    """
    conversation_id_field: str = "conversation_id"
    script_id_field: str = "script_id"
    history_field: str = "history"
    turn_field: str = "turn"
    role_field: str = "role"
    content_field: str = "content"
    # 角色名映射：key 是内部统一标识（如 "user""agent"），value 是轨迹中的实际值
    role_mapping: Dict[str, str] = None
    # 证据位置规则映射：key 是规则类型，value 是匹配关键词列表
    evidence_rules: Dict[str, List[str]] = None
    # 默认格式化的角色显示名（仅用于 prompt 展示）
    display_names: Dict[str, str] = None
    # 片段提取：默认取最近多少轮
    default_max_turns: int = 10
    # 片段提取：上下文窗口
    context_window: int = 2

    def __post_init__(self):
        if self.role_mapping is None:
            self.role_mapping = {"user": "customer", "agent": "agent"}
        if self.evidence_rules is None:
            self.evidence_rules = {
                "last": ["最后", "末尾", "结束", "last", "final", "end"],
                "user_turns": ["用户输入", "用户", "user", "customer", "sender"],
                "agent_turns": ["agent回复", "agent", "assistant", "助手", "系统", "bot", "回复"],
            }
        if self.display_names is None:
            self.display_names = {"user": "用户", "agent": "Agent"}

    def get_role_value(self, internal_role: str) -> str:
        """获取内部角色（user/agent）在轨迹中的实际值"""
        return self.role_mapping.get(internal_role, internal_role)

    def get_role_from_value(self, value: str) -> Optional[str]:
        """从轨迹中的实际值反推内部角色"""
        for internal, actual in self.role_mapping.items():
            if actual == value:
                return internal
        return None

    def is_role(self, turn: Dict, internal_role: str) -> bool:
        """判断某轮是否属于指定内部角色"""
        actual_value = turn.get(self.role_field, "")
        return actual_value == self.get_role_value(internal_role)

    def format_turn(self, turn: Dict) -> str:
        """格式化单轮对话文本"""
        role_val = turn.get(self.role_field, "?")
        internal_role = self.get_role_from_value(role_val)
        display_name = self.display_names.get(internal_role, role_val)
        turn_num = turn.get(self.turn_field, "?")
        content = turn.get(self.content_field, "")
        return f"  第{turn_num}轮 [{display_name}]: {content}"

    def extract_relevant_turns(self, history: List[Dict], evidence_location: str = "",
                                max_turns: int = None, context_window: int = None) -> List[Dict]:
        """从轨迹中提取与检查点相关的对话轮次"""
        if not history:
            return []

        max_turns = max_turns or self.default_max_turns
        context_window = context_window or self.context_window
        loc = evidence_location.lower()

        # 规则1：最后 N 轮
        for keyword in self.evidence_rules.get("last", []):
            if keyword.lower() in loc:
                return history[-max_turns:]

        # 规则2：用户轮次 + 上下文
        for keyword in self.evidence_rules.get("user_turns", []):
            if keyword.lower() in loc:
                relevant_indices = set()
                for i, turn in enumerate(history):
                    if self.is_role(turn, "user"):
                        relevant_indices.add(i)
                        for j in range(max(0, i - context_window), min(len(history), i + context_window + 1)):
                            relevant_indices.add(j)
                return [history[i] for i in sorted(relevant_indices)]

        # 规则3：agent 轮次 + 上下文
        for keyword in self.evidence_rules.get("agent_turns", []):
            if keyword.lower() in loc:
                relevant_indices = set()
                for i, turn in enumerate(history):
                    if self.is_role(turn, "agent"):
                        relevant_indices.add(i)
                        for j in range(max(0, i - context_window), min(len(history), i + context_window + 1)):
                            relevant_indices.add(j)
                return [history[i] for i in sorted(relevant_indices)]

        # 默认：最后 N 轮
        return history[-max_turns:]


@dataclass
class Trajectory:
    conversation_id: str
    script_id: str
    history: List[Dict[str, Any]]


# ==================== LLM 判定 Prompt ====================

LLM_JUDGE_SYSTEM_PROMPT = """你是一个严格的对话质量判定员。你的任务是根据给定的「检查点定义」，对一段对话轨迹进行判定，输出 True/False/NA。

判定规则：
- 1 (True)：轨迹中的行为满足检查点定义（通过）
- 0 (False)：轨迹中的行为违反检查点定义（未通过）
- NA：轨迹中未出现该检查点相关的场景（不适用）

判定标准：
- 严格遵循检查点定义，不要自行扩展或放宽
- 只基于给定的对话文本做判定，不要引入外部知识
- 如果文本中证据不足，优先判 NA 而非猜测

输出格式（严格 JSON，不要多余文字）：
{
  "result": 0或1或"NA",
  "reason": "判定理由（不超过30字）",
  "confidence": 0.0-1.0
}"""


# ==================== 轨迹加载（完全基于配置）====================

def load_trajectory_config(config_path: Optional[str]) -> TrajectoryConfig:
    """加载轨迹结构配置"""
    if config_path and Path(config_path).exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        return TrajectoryConfig(
            conversation_id_field=config_data.get("conversation_id_field", "conversation_id"),
            script_id_field=config_data.get("script_id_field", "script_id"),
            history_field=config_data.get("history_field", "history"),
            turn_field=config_data.get("turn_field", "turn"),
            role_field=config_data.get("role_field", "role"),
            content_field=config_data.get("content_field", "content"),
            role_mapping=config_data.get("role_mapping", None),
            evidence_rules=config_data.get("evidence_rules", None),
            display_names=config_data.get("display_names", None),
            default_max_turns=config_data.get("default_max_turns", 10),
            context_window=config_data.get("context_window", 2),
        )
    return TrajectoryConfig()


def load_trajectories(path: str, config: TrajectoryConfig) -> List[Trajectory]:
    """加载轨迹 —— 支持 .jsonl 文件、.json 文件、目录（批量读取 *.json）"""
    trajectories = []
    h = config.history_field
    cid = config.conversation_id_field
    sid = config.script_id_field

    if os.path.isfile(path):
        if path.endswith('.jsonl'):
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        trajectories.append(Trajectory(
                            conversation_id=data.get(cid, ""),
                            script_id=data.get(sid, ""),
                            history=data.get(h, [])
                        ))
        elif path.endswith('.json'):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    trajectories.append(Trajectory(
                        conversation_id=item.get(cid, ""),
                        script_id=item.get(sid, ""),
                        history=item.get(h, [])
                    ))
    elif os.path.isdir(path):
        for json_file in sorted(Path(path).glob('*.json')):
            with open(json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                trajectories.append(Trajectory(
                    conversation_id=data.get(cid, ""),
                    script_id=data.get(sid, ""),
                    history=data.get(h, [])
                ))
    return trajectories


# ==================== 片段格式化（基于配置）====================

def format_trajectory_snippet(turns: List[Dict], config: TrajectoryConfig) -> str:
    """格式化轨迹片段 —— 基于配置的角色名和内容字段"""
    lines = []
    for turn in turns:
        lines.append(config.format_turn(turn))
    return "\n".join(lines)


def extract_relevant_turns(history: List[Dict], evidence_location: str,
                          config: TrajectoryConfig, max_turns: int = 10) -> List[Dict]:
    """基于配置提取相关片段"""
    return config.extract_relevant_turns(history, evidence_location, max_turns)


# ==================== LLM 判定核心 ====================

class LLMJudgeCache:
    """缓存 LLM 判定结果"""

    def __init__(self):
        self.cache: Dict[str, Dict] = {}

    def _make_key(self, checkpoint_id: str, trajectory_id: str,
                  turns_hash: str, criteria_hash: str) -> str:
        raw = f"{checkpoint_id}:{trajectory_id}:{turns_hash}:{criteria_hash}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _criteria_hash(self, checkpoint: 'CheckpointDef') -> str:
        """将 checkpoint 定义纳入缓存 key，避免 criteria 修改后命中旧缓存"""
        raw = json.dumps({
            'description': checkpoint.description,
            'judgment_criteria': checkpoint.judgment_criteria,
            'evidence_location': checkpoint.evidence_location,
        }, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, checkpoint: 'CheckpointDef', trajectory_id: str, turns: List[Dict]) -> Optional[Dict]:
        turns_hash = hashlib.md5(json.dumps(turns, sort_keys=True).encode()).hexdigest()
        criteria_hash = self._criteria_hash(checkpoint)
        key = self._make_key(checkpoint.checkpoint_id, trajectory_id, turns_hash, criteria_hash)
        return self.cache.get(key)

    def set(self, checkpoint: 'CheckpointDef', trajectory_id: str, turns: List[Dict], result: Dict):
        turns_hash = hashlib.md5(json.dumps(turns, sort_keys=True).encode()).hexdigest()
        criteria_hash = self._criteria_hash(checkpoint)
        key = self._make_key(checkpoint.checkpoint_id, trajectory_id, turns_hash, criteria_hash)
        self.cache[key] = result

    def save(self, path: str):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)

    def load(self, path: str):
        if Path(path).exists():
            with open(path, 'r', encoding='utf-8') as f:
                self.cache.update(json.load(f))


def build_single_judge_prompt(checkpoint: CheckpointDef, trajectory: Trajectory,
                              config: TrajectoryConfig, max_turns: int = 10) -> Tuple[str, List[Dict]]:
    """为单个 checkpoint 构建判定 prompt"""
    turns = extract_relevant_turns(trajectory.history, checkpoint.evidence_location, config, max_turns)
    snippet = format_trajectory_snippet(turns, config)

    prompt = f"""【检查点定义】
ID: {checkpoint.checkpoint_id}
描述: {checkpoint.description}
判定标准: {checkpoint.judgment_criteria}

【对话轨迹】
对话ID: {trajectory.conversation_id}
{snippet}

【判定要求】
请根据检查点定义，严格判定上述轨迹是否满足该检查点。
输出 JSON 格式: {{"result": 0/1/"NA", "reason": "理由", "confidence": 0.0-1.0}}
"""
    return prompt, turns


def parse_llm_judge_result(raw: str) -> Dict:
    """解析 LLM 判定结果"""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and 'result' in data:
            return {
                'result': data.get('result'),
                'reason': data.get('reason', ''),
                'confidence': data.get('confidence', 0.5)
            }
    except:
        pass

    code_blocks = re.findall(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if not code_blocks:
        code_blocks = re.findall(r'```\s*(.*?)\s*```', raw, re.DOTALL)
    for block in code_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and 'result' in data:
                return {
                    'result': data.get('result'),
                    'reason': data.get('reason', ''),
                    'confidence': data.get('confidence', 0.5)
                }
        except:
            pass

    match = re.search(r'\{[^\}]*"result"[^\}]*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return {
                'result': data.get('result'),
                'reason': data.get('reason', ''),
                'confidence': data.get('confidence', 0.5)
            }
        except:
            pass

    return {'result': 'NA', 'reason': '解析失败', 'confidence': 0.0, 'parse_error': True, 'raw': raw}


def judge_single_checkpoint(checkpoint: CheckpointDef, trajectory: Trajectory,
                             config: TrajectoryConfig, cache: LLMJudgeCache,
                             log_file=None, max_turns: int = 10) -> Dict:
    """对单个 checkpoint + 单条轨迹做 LLM 判定"""
    turns = extract_relevant_turns(trajectory.history, checkpoint.evidence_location, config, max_turns)
    cached = cache.get(checkpoint, trajectory.conversation_id, turns)
    if cached is not None:
        return cached

    prompt, used_turns = build_single_judge_prompt(checkpoint, trajectory, config, max_turns)
    messages = [
        {"role": "system", "content": LLM_JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt}
    ]
    raw = call_llm(messages)
    result = parse_llm_judge_result(raw)

    if log_file:
        log_entry = {
            'checkpoint_id': checkpoint.checkpoint_id,
            'conversation_id': trajectory.conversation_id,
            'prompt': prompt,
            'raw_response': raw,
            'parsed_result': result,
            'turns_used': len(used_turns),
            'turns_config': {
                'role_field': config.role_field,
                'content_field': config.content_field,
                'role_mapping': config.role_mapping,
            }
        }
        log_file.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        log_file.flush()

    cache.set(checkpoint, trajectory.conversation_id, turns, result)
    return result


def judge_batch_checkpoints(checkpoints: List[CheckpointDef], trajectory: Trajectory,
                             config: TrajectoryConfig, cache: LLMJudgeCache,
                             log_file=None, max_turns: int = 10, batch_size: int = 5) -> Dict[str, Dict]:
    """批量判定：一次 LLM 请求判多个 checkpoints"""
    results = {}
    uncached = []

    for cp in checkpoints:
        turns = extract_relevant_turns(trajectory.history, cp.evidence_location, config, max_turns)
        cached = cache.get(cp, trajectory.conversation_id, turns)
        if cached is not None:
            results[cp.checkpoint_id] = cached
        else:
            uncached.append(cp)

    if not uncached:
        return results

    for batch_start in range(0, len(uncached), batch_size):
        batch = uncached[batch_start:batch_start + batch_size]
        cp_defs = []
        for i, cp in enumerate(batch, 1):
            cp_defs.append(f"""
检查点 {i}:
ID: {cp.checkpoint_id}
描述: {cp.description}
判定标准: {cp.judgment_criteria}
""")

        # 按每个 checkpoint 的 evidence_location 独立提取片段，合并去重
        all_turn_indices = set()
        per_cp_turns = {}
        for cp in batch:
            cp_turns = extract_relevant_turns(trajectory.history, cp.evidence_location, config, max_turns)
            per_cp_turns[cp.checkpoint_id] = cp_turns
            for i, turn in enumerate(trajectory.history):
                if turn in cp_turns:
                    all_turn_indices.add(i)
        # 按原始顺序合并并保留上下文
        merged_turns = [trajectory.history[i] for i in sorted(all_turn_indices)]
        snippet = format_trajectory_snippet(merged_turns, config)

        prompt = f"""【判定任务】
以下有 {len(batch)} 个检查点，请逐一对给定的对话轨迹进行判定。

【对话轨迹】
对话ID: {trajectory.conversation_id}
{snippet}

【检查点定义】
{''.join(cp_defs)}

【输出要求】
对每个检查点，输出 JSON 判定结果。格式如下：
{{
  "{batch[0].checkpoint_id}": {{"result": 0/1/"NA", "reason": "理由", "confidence": 0.0-1.0}},
  ...（每个检查点一个键）
}}

注意：
- 只输出 JSON，不要任何其他文字
- 严格根据每个检查点的定义独立判定，不要互相影响
- 如果轨迹中未出现该检查点相关场景，判 "NA"
"""

        messages = [
            {"role": "system", "content": LLM_JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        raw = call_llm(messages)

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                for cp in batch:
                    cp_result = parsed.get(cp.checkpoint_id)
                    if cp_result and isinstance(cp_result, dict):
                        result = {
                            'result': cp_result.get('result'),
                            'reason': cp_result.get('reason', ''),
                            'confidence': cp_result.get('confidence', 0.5)
                        }
                    else:
                        result = {'result': 'NA', 'reason': '批量解析中未找到该检查点', 'confidence': 0.0}
                    results[cp.checkpoint_id] = result
                    cache.set(cp, trajectory.conversation_id, per_cp_turns[cp.checkpoint_id], result)
            else:
                for cp in batch:
                    single_result = judge_single_checkpoint(cp, trajectory, config, cache, log_file, max_turns)
                    results[cp.checkpoint_id] = single_result
        except json.JSONDecodeError:
            for cp in batch:
                single_result = judge_single_checkpoint(cp, trajectory, config, cache, log_file, max_turns)
                results[cp.checkpoint_id] = single_result

        if log_file:
            log_entry = {
                'batch': True,
                'conversation_id': trajectory.conversation_id,
                'checkpoint_ids': [cp.checkpoint_id for cp in batch],
                'prompt': prompt,
                'raw_response': raw,
                'parsed_results': {cpid: results[cpid] for cpid in [cp.checkpoint_id for cp in batch]},
                'turns_config': {
                    'role_field': config.role_field,
                    'content_field': config.content_field,
                    'role_mapping': config.role_mapping,
                }
            }
            log_file.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            log_file.flush()

    return results


# ==================== 主流程 ====================

def load_checkpoint_defs(phase3_path: str) -> List[CheckpointDef]:
    with open(phase3_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    phase3 = data.get('phase3_output', data)
    checkpoints = []
    for cat in phase3.get('categories', []):
        cat_id = cat.get('category_id', '')
        for chk in cat.get('binary_checkpoints', []):
            cp = CheckpointDef(
                checkpoint_id=chk.get('checkpoint_id', ''),
                category_id=cat_id,
                description=chk.get('description', ''),
                judgment_criteria=chk.get('judgment_criteria', ''),
                evidence_location=chk.get('evidence_location', '')
            )
            if cp.checkpoint_id:
                checkpoints.append(cp)
    return checkpoints


def extract_features(trajectories: List[Trajectory],
                     checkpoints: List[CheckpointDef],
                     config: TrajectoryConfig,
                     use_batch: bool = True,
                     batch_size: int = 5,
                     max_turns: int = 10,
                     cache: LLMJudgeCache = None,
                     log_file=None,
                     progress_interval: int = 10) -> List[Dict]:
    """为所有轨迹提取所有 checkpoint 特征"""
    if cache is None:
        cache = LLMJudgeCache()

    results = []
    total_calls = 0
    cached_hits = 0

    for t_idx, traj in enumerate(trajectories):
        features = {
            'conversation_id': traj.conversation_id,
            'script_id': traj.script_id,
        }

        if use_batch and len(checkpoints) > 1:
            cp_results = judge_batch_checkpoints(
                checkpoints, traj, config, cache, log_file,
                max_turns=max_turns, batch_size=batch_size
            )
            for cp_id, cp_result in cp_results.items():
                features[f'{cp_id}_final'] = cp_result['result']
                features[f'{cp_id}_reason'] = cp_result['reason']
                features[f'{cp_id}_confidence'] = cp_result['confidence']
            total_calls += max(1, len(checkpoints) // batch_size)
        else:
            for cp in checkpoints:
                turns = extract_relevant_turns(traj.history, cp.evidence_location, config, max_turns)
                cached = cache.get(cp, traj.conversation_id, turns)
                if cached:
                    cached_hits += 1
                    result = cached
                else:
                    result = judge_single_checkpoint(cp, traj, config, cache, log_file, max_turns)
                    total_calls += 1

                features[f'{cp.checkpoint_id}_final'] = result['result']
                features[f'{cp.checkpoint_id}_reason'] = result['reason']
                features[f'{cp.checkpoint_id}_confidence'] = result['confidence']

        results.append(features)

        if (t_idx + 1) % progress_interval == 0:
            print(f"  已处理 {t_idx + 1}/{len(trajectories)} 条轨迹 (LLM调用: {total_calls}次, 缓存命中: {cached_hits}次)")

    return results


def generate_report(features: List[Dict], checkpoints: List[CheckpointDef]) -> Dict:
    report = {
        'total_trajectories': len(features),
        'checkpoint_stats': {}
    }
    for cp in checkpoints:
        cp_id = cp.checkpoint_id
        final_key = f'{cp_id}_final'
        values = [f.get(final_key, 'NA') for f in features]
        counter = Counter(values)
        report['checkpoint_stats'][cp_id] = {
            'description': cp.description[:80],
            'pass': counter.get(1, 0),
            'fail': counter.get(0, 0),
            'na': counter.get('NA', 0),
            'pass_rate': counter.get(1, 0) / len(features) if len(features) > 0 else 0
        }
    return report


def print_report(report: Dict):
    print("\n" + "=" * 60)
    print("【Phase 4 v3 LLM 特征提取报告】")
    print("=" * 60)
    print(f"\n总轨迹数: {report['total_trajectories']}")
    for cp_id, stats in report['checkpoint_stats'].items():
        print(f"\n  {cp_id}: {stats['description']}")
        print(f"    通过: {stats['pass']} ({stats['pass_rate']:.1%}) | 违反: {stats['fail']} | 不适用: {stats['na']}")
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="阶段4 v3：配置化 LLM 特征提取")
    parser.add_argument("--trajectories", type=str, required=True, help="轨迹文件 (.jsonl/.json) 或目录")
    parser.add_argument("--categories", type=str, required=True, help="phase3 类别定义")
    parser.add_argument("--trajectory-config", type=str, default="",
                        help="轨迹结构配置 JSON（默认兼容 customer/agent 格式）")
    parser.add_argument("--output", type=str, default="features.jsonl")
    parser.add_argument("--report", type=str, default="feature_report.json")
    parser.add_argument("--log", type=str, default="llm_judge_logs.jsonl", help="LLM判定日志")
    parser.add_argument("--cache", type=str, default="llm_judge_cache.json", help="缓存文件")
    parser.add_argument("--batch-size", type=int, default=5, help="批量判定每批检查点数")
    parser.add_argument("--max-turns", type=int, default=10, help="每条轨迹提取的最大轮数")
    parser.add_argument("--no-batch", action="store_true", help="禁用批量模式")
    args = parser.parse_args()

    # 加载配置
    config = load_trajectory_config(args.trajectory_config)
    print(f"轨迹配置: role_field={config.role_field}, content_field={config.content_field}")
    print(f"角色映射: {config.role_mapping}")
    print(f"证据规则: { {k: v[:3] for k, v in config.evidence_rules.items()} }")

    # 加载数据
    trajectories = load_trajectories(args.trajectories, config)
    checkpoints = load_checkpoint_defs(args.categories)
    print(f"\n加载 {len(trajectories)} 条轨迹，{len(checkpoints)} 个检查点")

    # 缓存
    cache = LLMJudgeCache()
    if args.cache:
        cache.load(args.cache)
        print(f"缓存加载: {len(cache.cache)} 条")

    # 日志
    log_file = None
    if args.log:
        log_file = open(args.log, 'w', encoding='utf-8')

    # 提取
    mode = 'individual' if args.no_batch else 'batch'
    print(f"\n开始提取（模式: {mode}, batch_size={args.batch_size}, max_turns={args.max_turns}）...")

    features = extract_features(
        trajectories, checkpoints, config,
        use_batch=(mode == 'batch'),
        batch_size=args.batch_size,
        max_turns=args.max_turns,
        cache=cache,
        log_file=log_file
    )

    if log_file:
        log_file.close()

    if args.cache:
        cache.save(args.cache)
        print(f"\n缓存已保存: {args.cache}")

    # 保存
    with open(args.output, 'w', encoding='utf-8') as f:
        for feat in features:
            f.write(json.dumps(feat, ensure_ascii=False) + '\n')

    report = generate_report(features, checkpoints)
    with open(args.report, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n特征已保存: {args.output}")
    print(f"报告已保存: {args.report}")
    if args.log:
        print(f"日志已保存: {args.log}")
    print_report(report)

    print("\n提示:")
    print("  1. 查看 llm_judge_logs.jsonl 可审计每条判定理由")
    print("  2. 如果某检查点 confidence 普遍偏低，可能需要优化其 judgment_criteria 描述")
    print("  3. 换场景时：修改 --trajectory-config 即可，代码完全不动")


if __name__ == "__main__":
    main()
