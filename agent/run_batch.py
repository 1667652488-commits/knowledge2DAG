# -*- coding: utf-8 -*-
"""两档跑批脚本：τ-bench retail × 强/弱档 → 原始轨迹 + adapter 转换。

批次目录规约（见 docs/实施方案/实施方案.md §5）：
    runs/<ts>_<tier>/
      raw/            τ-bench 原始结果 JSON（traj + reward）
      traces/         adapter 转换后的 messages-only 轨迹
      tau_reward.json 硬判分锚点
      batch_meta.json 本批配置（tier/model/ablation/trials 等）

档位：
  strong  完整政策手册（wiki 全文进 system prompt）
  weak    删节政策手册（--ablate 指定要删的 wiki 小节标题，可多个），制造政策违例 badcase

prompt 模式（--prompt-mode，默认 skill）：
  wiki   现状：env WIKI = 完整 wiki 全文（向后兼容；--ablate 仅本模式可用）
  skill  env WIKI = agent/prompts/global_rules.md（wiki 全局部分英文原文）
         + "\n\n# Skills\n" + data/skills_flat/*.md 顺序拼接（原样含 frontmatter，
         文件间加 '## Skill: <name>' 分隔标题）。agent 行为与 skill 文档单一事实源。
         注意：skill 模式下 --tier 语义退化为仅影响批次目录名。
  故障注入：--corrupt-skill <skill名> --corrupt-with <替换文件路径>（仅 skill 模式），
  组装时用替换文件内容代替原 skill .md，制造定点 badcase。

LLM 配置：读 agent/.env（KEY=VALUE，不覆盖已存在 env）。OpenAI 兼容端点：
  OPENAI_API_KEY / OPENAI_BASE_URL + 模型名形如 "openai/glm-x"
DeepSeek 官方：DEEPSEEK_API_KEY + 模型名形如 "deepseek/deepseek-chat"

用法（agent/.venv 环境）：
    agent/.venv/Scripts/python agent/run_batch.py --tier strong \
        --model openai/glm-x --user-model openai/glm-x \
        --start 0 --end 10 --num-trials 1 --max-concurrency 4
    agent/.venv/Scripts/python agent/run_batch.py --tier weak \
        --model openai/glm-x --ablate "## Cancel pending order" --ablate "## Return delivered order"
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 本脚本与 tau_bench 仓库目录同在 agent/ 下，脚本目录会进 sys.path[0]，
# 导致 "import tau_bench" 被仓库目录（namespace package）遮蔽、与 repo 根 run.py 循环 import。
# 这里先把脚本目录从 sys.path 剔除，让 import 落到 pip -e 安装的包上。
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [p for p in sys.path if Path(p or ".").resolve() != _SCRIPT_DIR]

ROOT = _SCRIPT_DIR.parent
AGENT_DIR = ROOT / "agent"
ADAPTER = ROOT / "adapter" / "tau2bundle.py"
TASKS = AGENT_DIR / "tau_bench" / "tau_bench" / "envs" / "retail" / "tasks_test.py"
RUNS = ROOT / "runs"
GLOBAL_RULES = AGENT_DIR / "prompts" / "global_rules.md"
SKILLS_DIR = ROOT / "data" / "skills_flat"


def build_skill_prompt(corrupt_skill: str = None, corrupt_with: str = None) -> str:
    """组装 skill 模式 prompt：global_rules.md + 全部 skills_flat/*.md 顺序拼接。

    每个 skill 文件原样保留（含 frontmatter），文件前加 '## Skill: <name>' 分隔标题。
    corrupt_skill/corrupt_with：用替换文件内容代替名为 corrupt_skill 的 skill 文档。
    """
    parts = [GLOBAL_RULES.read_text(encoding="utf-8").rstrip(), "# Skills"]
    for md in sorted(SKILLS_DIR.glob("*.md")):
        name = md.stem
        if corrupt_skill and name == corrupt_skill:
            content = Path(corrupt_with).read_text(encoding="utf-8").rstrip()
            print(f"  corrupt-skill '{name}': 用 {corrupt_with} 替换原 skill 文档")
        else:
            content = md.read_text(encoding="utf-8").rstrip()
        parts.append(f"## Skill: {name}\n\n{content}")
    return "\n\n".join(parts) + "\n"


def load_dotenv() -> None:
    """读 agent/.env 注入环境（不覆盖已存在的 env）。

    若 agent/.env 不存在，回退读 sut/.env 并把 LLM_* 映射为 litellm 需要的
    OPENAI_API_KEY / OPENAI_BASE_URL（sut 为 OpenAI 兼容模式时）。
    """
    def _load(path: Path) -> dict:
        kv = {}
        if not path.exists():
            return kv
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip().strip('"').strip("'")
        return kv

    for k, v in _load(AGENT_DIR / ".env").items():
        if k and k not in os.environ:
            os.environ[k] = v
    if "OPENAI_API_KEY" not in os.environ:
        sut_kv = _load(ROOT / "sut" / ".env")
        mapping = {"LLM_API_KEY": "OPENAI_API_KEY", "LLM_BASE_URL": "OPENAI_BASE_URL"}
        for src, dst in mapping.items():
            if sut_kv.get(src) and dst not in os.environ:
                os.environ[dst] = sut_kv[src]
        if "OPENAI_API_KEY" in os.environ:
            print("[info] 已从 sut/.env 映射 LLM_* → OPENAI_* 供 litellm 使用")


def ablate_wiki(wiki: str, sections: list) -> str:
    """按 markdown 小节标题删节政策手册。标题匹配到下一同级标题为止。"""
    out = wiki
    for sec in sections:
        lines = out.splitlines()
        new_lines, skipping = [], False
        for ln in lines:
            if ln.strip() == sec.strip():
                skipping = True
                continue
            if skipping and ln.startswith("## "):
                skipping = False
            if not skipping:
                new_lines.append(ln)
        removed = len(lines) - len(new_lines)
        print(f"  ablate '{sec}': 删除 {removed} 行")
        out = "\n".join(new_lines)
    return out


def main() -> None:
    # tau_bench run.py 会 print ✅/❌ emoji，GBK 控制台会 UnicodeEncodeError，统一改 utf-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=["strong", "weak"])
    ap.add_argument("--model", required=True, help="litellm 模型名，如 openai/glm-x 或 deepseek/deepseek-chat")
    ap.add_argument("--user-model", default=None, help="用户模拟器模型，默认同 --model")
    ap.add_argument("--model-provider", default="openai")
    ap.add_argument("--user-model-provider", default=None)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=-1)
    ap.add_argument("--task-ids", type=int, nargs="+", default=None)
    ap.add_argument("--num-trials", type=int, default=1)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-concurrency", type=int, default=4)
    ap.add_argument("--ablate", action="append", default=[],
                    help="弱档：要删节的 wiki 小节标题（如 '## Cancel pending order'），可多次；仅 wiki 模式可用")
    ap.add_argument("--prompt-mode", choices=["wiki", "skill"], default="skill",
                    help="wiki=完整 wiki 全文（向后兼容）；skill=global_rules+skills_flat 拼接（默认）")
    ap.add_argument("--corrupt-skill", default=None,
                    help="skill 模式故障注入：要替换的 skill 名（不含 .md），需配合 --corrupt-with")
    ap.add_argument("--corrupt-with", default=None,
                    help="skill 模式故障注入：替换文件路径，其内容代替原 skill 文档进 prompt")
    ap.add_argument("--suffix", default="", help="批次目录附加后缀（如 inject_xxx）")
    args = ap.parse_args()

    # 参数互斥校验
    if args.prompt_mode == "wiki" and (args.corrupt_skill or args.corrupt_with):
        ap.error("--corrupt-skill/--corrupt-with 仅 skill 模式可用")
    if args.prompt_mode == "skill" and args.ablate:
        ap.error("--ablate 仅 wiki 模式可用；skill 模式故障注入请用 --corrupt-skill/--corrupt-with")
    if bool(args.corrupt_skill) != bool(args.corrupt_with):
        ap.error("--corrupt-skill 与 --corrupt-with 必须同时指定")

    load_dotenv()
    user_model = args.user_model or args.model
    user_provider = args.user_model_provider or args.model_provider

    # 批次目录
    ts = time.strftime("%Y%m%d_%H%M%S")
    batch_name = f"{ts}_{args.tier}{'_' + args.suffix if args.suffix else ''}"
    batch_dir = RUNS / batch_name
    raw_dir = batch_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 弱档：删节政策手册（patch env 模块里运行时绑定的 WIKI 全局名）
    if args.tier == "weak":
        if not args.ablate:
            print("[warn] weak 档但未指定 --ablate，政策手册为全文（等价 strong 弱模型）")
        if args.prompt_mode == "wiki":
            import tau_bench.envs.retail.env as retail_env
            from tau_bench.envs.retail.wiki import WIKI
            retail_env.WIKI = ablate_wiki(WIKI, args.ablate)
    # skill 模式：WIKI = global_rules + skills 拼接（--tier 在此模式下语义退化为仅影响批次目录名）
    if args.prompt_mode == "skill":
        import tau_bench.envs.retail.env as retail_env
        retail_env.WIKI = build_skill_prompt(args.corrupt_skill, args.corrupt_with)

    from tau_bench.run import run
    from tau_bench.types import RunConfig

    config = RunConfig(
        model_provider=args.model_provider,
        user_model_provider=user_provider,
        model=args.model,
        user_model=user_model,
        num_trials=args.num_trials,
        env="retail",
        agent_strategy="tool-calling",
        temperature=args.temperature,
        task_split="test",
        start_index=args.start,
        end_index=args.end,
        task_ids=args.task_ids,
        log_dir=str(raw_dir),
        max_concurrency=args.max_concurrency,
        seed=10,
        shuffle=0,
        user_strategy="llm",
        few_shot_displays_path=None,
    )
    print(f"批次目录: {batch_dir}")
    print(f"开始跑批: tier={args.tier} prompt_mode={args.prompt_mode} model={args.model} "
          f"trials={args.num_trials} "
          f"range=[{args.start},{args.end}) task_ids={args.task_ids}")
    results = run(config)

    # 批次元信息
    rewards = {f"tau_{r.task_id}_t{r.trial}": r.reward for r in results}
    meta = {
        "tier": args.tier, "model": args.model, "user_model": user_model,
        "num_trials": args.num_trials, "temperature": args.temperature,
        "start": args.start, "end": args.end, "task_ids": args.task_ids,
        "ablate": args.ablate, "prompt_mode": args.prompt_mode,
        "corrupt_skill": args.corrupt_skill, "corrupt_with": args.corrupt_with,
        "n_results": len(results),
        "reward_mean": sum(rewards.values()) / len(rewards) if rewards else None,
    }
    (batch_dir / "batch_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"平均 reward: {meta['reward_mean']}")

    # adapter 转换（原始结果 JSON → messages-only traces + tau_reward.json）
    raw_files = sorted(raw_dir.glob("*.json"))
    if not raw_files:
        print("[warn] raw/ 下无结果文件，跳过 adapter 转换")
        return
    cmd = [sys.executable, str(ADAPTER)]
    for rf in raw_files:
        cmd += ["--results", str(rf)]
    cmd += ["--tasks", str(TASKS), "--out-dir", str(batch_dir / "traces")]
    print("调用 adapter:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))
    # adapter 默认把 tau_reward.json 落在 out-dir 上一级 = batch_dir，符合规约
    print(f"[完成] 批次产物: {batch_dir}")


if __name__ == "__main__":
    sys.exit(main())
