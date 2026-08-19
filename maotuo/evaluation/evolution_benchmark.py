"""受控自进化候选的轻量真实 A/B 评测入口。

本模块只消费固定的 EvolutionCase 和真实代码任务，不修改候选审批状态。
它的消费者是简历数据、人工审批和回归报告；调用方是命令行或后续 CI。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .real_benchmark import DEFAULT_REAL_BENCHMARK, run_real_benchmark
from .replay import run_golden_tasks


DEFAULT_CASES = Path("benchmarks/maotuo_evolution_cases.json")
DEFAULT_OUTPUT = Path("artifacts/maotuo-evolution-v1")
DEFAULT_STRATEGY = "先读取相关文件，修改后运行针对性测试，并在回答前复核最新证据。"


def _load_cases(path):
    """读取并校验训练集与封闭集任务映射。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = list(payload.get("cases", []))
    if not cases:
        raise ValueError("evolution case set is empty")
    if any(not str(case.get("task_id", "")).strip() for case in cases):
        raise ValueError("every evolution case must map to a real task")
    return payload, cases


def _split_task_ids(cases, split):
    """返回指定 split 的去重任务 ID，保持案例文件中的稳定顺序。"""
    result = []
    for case in cases:
        if str(case.get("split")) != split:
            continue
        task_id = str(case["task_id"])
        if task_id not in result:
            result.append(task_id)
    return result


def _acceptance(train_before, train_after, holdout_before, holdout_after, golden):
    """按固定门槛判断候选是否值得提交人工审批。"""
    train_improved = train_after["summary"]["pass_rate"] > train_before["summary"]["pass_rate"]
    holdout_safe = holdout_after["summary"]["pass_rate"] >= holdout_before["summary"]["pass_rate"]
    golden_passed = int(golden.get("passed", 0)) == int(golden.get("total", 0)) and int(golden.get("total", 0)) > 0
    return {
        "training_improved": train_improved,
        "holdout_not_regressed": holdout_safe,
        "golden_passed": golden_passed,
        "approval_ready": bool(train_improved and holdout_safe and golden_passed),
    }


def run_evolution_benchmark(
    cases_path=DEFAULT_CASES,
    benchmark_path=DEFAULT_REAL_BENCHMARK,
    output_root=DEFAULT_OUTPUT,
    provider="ollama",
    model=None,
    base_url=None,
    host="http://127.0.0.1:11434",
    temperature=0.0,
    top_p=1.0,
    timeout=300,
    repetitions=1,
    strategy=DEFAULT_STRATEGY,
):
    """对训练集和封闭集分别执行 Baseline/Candidate，并保存审批依据。"""
    payload, cases = _load_cases(cases_path)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    results = {}
    for split in ("train", "holdout"):
        task_ids = _split_task_ids(cases, split)
        before = run_real_benchmark(
            benchmark_path=benchmark_path,
            artifact_root=output_root / split / "baseline",
            provider=provider,
            model=model,
            base_url=base_url,
            host=host,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            repetitions=repetitions,
            mode="baseline",
            task_ids=task_ids,
        )
        after = run_real_benchmark(
            benchmark_path=benchmark_path,
            artifact_root=output_root / split / "candidate",
            provider=provider,
            model=model,
            base_url=base_url,
            host=host,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            repetitions=repetitions,
            mode="maotuo",
            task_ids=task_ids,
            # 该策略只在评测副本中注入，最终是否生效仍由人工审批决定。
            agent_options={"evaluation_prompt_strategy": strategy},
        )
        results[split] = {
            "task_ids": task_ids,
            "baseline": before,
            "candidate": after,
        }

    golden = run_golden_tasks()
    acceptance = _acceptance(
        results["train"]["baseline"],
        results["train"]["candidate"],
        results["holdout"]["baseline"],
        results["holdout"]["candidate"],
        golden,
    )
    report = {
        "schema_version": 1,
        "suite": "MaotuoEvolutionBenchmark",
        "cases": str(Path(cases_path)),
        "case_count": len(cases),
        "strategy": strategy,
        "golden": golden,
        "splits": results,
        "acceptance": acceptance,
        "note": "候选评测结果不等于人工审批；未通过 approval_ready 不得激活。",
    }
    (output_root / "evolution-summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def build_arg_parser():
    """构造自进化评测命令行参数。"""
    parser = argparse.ArgumentParser(description="Run Maotuo controlled evolution A/B benchmark.")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--benchmark", default=str(DEFAULT_REAL_BENCHMARK))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="ollama")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--strategy", default=DEFAULT_STRATEGY)
    return parser


def main(argv=None):
    """运行自进化候选 A/B 评测并输出 JSON 摘要。"""
    args = build_arg_parser().parse_args(argv)
    report = run_evolution_benchmark(
        cases_path=args.cases,
        benchmark_path=args.benchmark,
        output_root=args.output_root,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        host=args.host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.timeout,
        repetitions=args.repetitions,
        strategy=args.strategy,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
