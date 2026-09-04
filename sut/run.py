#!/usr/bin/env python3
"""
run.py — intranet_bundle 统一入口(纯 Python, 无需 skill 运行时)。

"skills/" 只是文件夹命名, 不是 Claude Code skill; 每个 skill 就是一个普通 Python CLI。
本入口把 bundle 根加入 sys.path, 让你从任何目录都能跑, 不用 python -m skills.xxx.run。

用法:
    python run.py <命令> [该命令的参数...]
    python run.py list            # 列出所有命令

示例:
    python run.py golden --trace-dir data/traces --regenerate-global
    python run.py coldstart --trace-dir data/traces --golden data/golden/golden_output.jsonl --skills data/skills_flat --output runs/coldstart
    python run.py rules --rules runs/coldstart/merged/final_rules.json --output data/rules_python --traces data/traces
    python run.py validate --rules-dir data/rules_python --trace-dir data/traces --golden data/golden/golden_output.jsonl
    python run.py trace_md --trace data/traces/trace_F05.json --output data/md/trace_F05.md

内网只需: Python 3.8-3.10 + pip install --no-index --find-links wheels/ -r requirements.txt + 可达 LLM 端点。
"""
import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE))

# 命令 → 模块 main
COMMANDS = {
    "script_gen":  "skills.badcase_script_gen.run",
    "agent":       "skills.agent_runner.run",
    "golden":      "skills.golden_gen.run",
    "coldstart":   "skills.coldstart_mining.run",
    "rules":       "skills.rules_gen.run",
    "validate":    "skills.validation.validate",
    "quality":     "skills.validation.quality_stats",
    "curation":    "skills.badcase_curation.run",
    "trace_md":    "skills.trace_md.run",
    "score":       "skills.scorer.run",
    "localize":    "skills.fault_localization.run",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("list", "--list", "-h", "--help"):
        print("可用命令:")
        for k, m in COMMANDS.items():
            print(f"  {k:12s} → {m}")
        print("\n用法: python run.py <命令> [参数...]")
        return

    cmd = sys.argv[1]
    if cmd not in COMMANDS:
        print(f"未知命令: {cmd}\n可用: {', '.join(COMMANDS)}")
        sys.exit(1)

    # 把命令本身去掉, 只传后续参数给 skill 的 main
    sys.argv = [cmd] + sys.argv[2:]
    mod = __import__(COMMANDS[cmd], fromlist=["main"])
    mod.main()


if __name__ == "__main__":
    main()
