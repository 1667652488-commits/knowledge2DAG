# -*- coding: utf-8 -*-
"""tau2bundle.py

把 tau-bench 跑批结果（EnvRunResult.model_dump() 的 JSON list）
转成 sut 工具链要求的 messages-only trace 格式。

用法:
    python adapter/tau2bundle.py --results <tau_bench结果.json> [--results 更多.json]
        --tasks <tasks_test.py路径> --out-dir <trace输出目录> [--keep-system]
"""

import argparse
import importlib.util
import json
import os
import re
import uuid


def load_instructions(tasks_path):
    """从 tasks_test.py 读取 instruction 列表（按 task 在 list 中的索引对应 task_id）。

    优先 importlib 直接加载模块读 TASKS_TEST；
    若 import 失败（例如 tau_bench 包依赖未安装），退化为正则解析 Task( 块。
    """
    try:
        spec = importlib.util.spec_from_file_location("tau_tasks", tasks_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        tasks = getattr(mod, "TASKS_TEST")
        return [t.instruction if hasattr(t, "instruction") else t["instruction"] for t in tasks]
    except Exception as e:
        print("[warn] import tasks 失败 ({}), 退化为正则解析".format(e))
    with open(tasks_path, "r", encoding="utf-8") as f:
        text = f.read()
    # 匹配 Task( ... instruction="..." 或 instruction='...'
    pattern = re.compile(
        r'Task\(.*?instruction\s*=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
        re.DOTALL,
    )
    instructions = []
    for m in pattern.finditer(text):
        raw = m.group(1)
        # 用 ast.literal_eval 处理转义
        import ast
        instructions.append(ast.literal_eval(raw))
    return instructions


def convert_message(msg, keep_system=False):
    """把 OpenAI/litellm 消息映射成 sut messages-only 格式。system 返回 None（跳过）。"""
    role = msg.get("role")
    if role == "system":
        if not keep_system:
            return None
        out = {
            "_class": "SystemMessage",
            "role": "system",
            "content": msg.get("content") or "",
            "name": None,
            "metadata": {"context_message_id": uuid.uuid4().hex},
        }
        return out
    class_map = {
        "user": "UserMessage",
        "assistant": "AssistantMessage",
        "tool": "ToolMessage",
    }
    if role not in class_map:
        return None
    out = {
        "_class": class_map[role],
        "role": role,
        "content": msg.get("content") or "",
        "name": None,
        "metadata": {"context_message_id": uuid.uuid4().hex},
    }
    if role == "assistant":
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            out["tool_calls"] = [
                {
                    "_class": "ToolCall",
                    "id": tc.get("id"),
                    "type": "function",
                    "name": (tc.get("function") or {}).get("name"),
                    "arguments": (tc.get("function") or {}).get("arguments"),
                    "index": i,
                    "response_item_id": None,
                }
                for i, tc in enumerate(tool_calls)
            ]
    if role == "tool":
        out["tool_call_id"] = msg.get("tool_call_id")
    return out


def main():
    parser = argparse.ArgumentParser(description="tau-bench results -> sut messages-only traces")
    parser.add_argument("--results", action="append", required=True,
                        help="tau-bench 结果 JSON 文件（可多次指定以合并）")
    parser.add_argument("--tasks", required=True, help="tasks_test.py 路径")
    parser.add_argument("--out-dir", required=True, help="trace 输出目录")
    parser.add_argument("--keep-system", action="store_true", help="保留 system 消息（默认跳过）")
    args = parser.parse_args()

    instructions = load_instructions(args.tasks)
    os.makedirs(args.out_dir, exist_ok=True)

    reward_path = os.path.join(os.path.dirname(os.path.abspath(args.out_dir.rstrip("/\\"))),
                               "tau_reward.json")
    reward_data = {}
    if os.path.exists(reward_path):
        with open(reward_path, "r", encoding="utf-8") as f:
            reward_data = json.load(f)

    n = 0
    for results_file in args.results:
        with open(results_file, "r", encoding="utf-8") as f:
            results = json.load(f)
        for item in results:
            task_id = item["task_id"]
            trial = item.get("trial", 0)
            reward = item.get("reward")
            raw_messages = item.get("traj") or item.get("messages") or []

            script_id = "tau_{}".format(task_id)
            instruction = ""
            if isinstance(task_id, int) and 0 <= task_id < len(instructions):
                instruction = instructions[task_id]
            else:
                print("[warn] task_id={} 超出任务列表范围, instruction 置空".format(task_id))

            messages = []
            for msg in raw_messages:
                converted = convert_message(msg, keep_system=args.keep_system)
                if converted is not None:
                    messages.append(converted)

            trace = {
                "script_id": script_id,
                "conversation_id": str(uuid.uuid4()),
                "script": {
                    "id": script_id,
                    "category": "tau_bench_retail",
                    "instruction": instruction,
                    "fixed_turns": [],
                    "pass_criteria": [],
                },
                "messages": messages,
                "errors": [],
            }
            out_file = os.path.join(args.out_dir, "trace_{}_t{}.json".format(task_id, trial))
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(trace, f, ensure_ascii=False, indent=2)

            reward_data["{}_t{}".format(script_id, trial)] = {
                "task_id": task_id,
                "trial": trial,
                "reward": reward,
            }
            n += 1

    with open(reward_path, "w", encoding="utf-8") as f:
        json.dump(reward_data, f, ensure_ascii=False, indent=2)

    print("共转换 {} 条轨迹 -> {}".format(n, args.out_dir))
    print("reward 文件: {}".format(reward_path))


if __name__ == "__main__":
    main()
