"""Maotuo 本地真实任务评测入口。

本模块不改变 AgentLoop 的执行逻辑，只负责把真实模型、任务工作区、
Baseline 对照和已有 BenchmarkEvaluator 组合起来，形成可复现的实验产物。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..cli import _build_model_client
from .evaluator import load_benchmark, run_fixed_benchmark
from .replay import run_golden_tasks


DEFAULT_REAL_BENCHMARK = Path("benchmarks/maotuo_real_tasks.json")
DEFAULT_REAL_ARTIFACT_ROOT = Path("artifacts/maotuo-real-v1")
ABLATION_MODES = ("baseline", "prompt_only", "prompt_context", "full")
# 小样本按任务类别分层抽取，避免简单的前序题把 Baseline 通过率顶满。
STRATIFIED_PILOT_TASK_IDS = (
    "core-no-input-mutation",
    "core-positive-task-id",
    "config-independent-defaults",
    "config-strict-coercion",
    "service-report-copy",
    "security-command-allowlist",
    "security-redaction-case-insensitive",
    "recovery-empty-filter",
)


def _provider_args(provider, model, base_url, host, temperature, top_p, timeout):
    """构造 Provider client 所需的最小参数对象。"""
    return argparse.Namespace(
        provider=str(provider),
        model=model or None,
        base_url=base_url or None,
        host=str(host),
        temperature=float(temperature),
        top_p=float(top_p),
        openai_timeout=int(timeout),
        ollama_timeout=int(timeout),
    )


def _build_factory(provider, model, base_url, host, temperature, top_p, timeout):
    """返回每个独立任务都重新创建模型 client 的工厂。"""
    args = _provider_args(provider, model, base_url, host, temperature, top_p, timeout)

    def factory(task, workspace):
        # 每个任务使用新 client，避免上一个任务的响应队列或状态污染当前任务。
        del task, workspace
        return _build_model_client(args)

    return factory


def _mode_options(mode):
    """返回四组消融配置；路径校验、审批和脱敏始终不参与关闭。"""
    normalized = str(mode or "").strip().lower()
    if normalized == "baseline":
        return {
            "feature_flags": {
                "memory": False,
                "relevant_memory": False,
                "context_reduction": False,
                "prompt_cache": False,
                "coding_prompt": False,
                "high_value_context": False,
                "evidence_chain": False,
                "release_gate": False,
            }
        }
    if normalized == "prompt_only":
        return {
            "feature_flags": {
                "memory": False,
                "relevant_memory": False,
                "context_reduction": False,
                "prompt_cache": False,
                "coding_prompt": True,
                "high_value_context": False,
                "evidence_chain": False,
                "release_gate": False,
            }
        }
    if normalized == "prompt_context":
        return {
            "feature_flags": {
                "memory": True,
                "relevant_memory": True,
                "context_reduction": True,
                "prompt_cache": True,
                "coding_prompt": True,
                "high_value_context": True,
                "evidence_chain": False,
                "release_gate": False,
            }
        }
    if normalized in {"full", "maotuo"}:
        # ``maotuo`` 保留为旧命令兼容名，新的报告统一显示为 Full。
        return {"feature_flags": {}}
    raise ValueError(f"unsupported benchmark mode: {mode}")


def _summary(artifacts):
    """汇总多个重复实验的任务和安全相关统计。"""
    rows = [row for artifact in artifacts for row in artifact.get("rows", [])]
    total = len(rows)
    passed = sum(bool(row.get("passed")) for row in rows)
    verifier_passed = sum(bool(row.get("verifier_passed")) for row in rows)
    within_budget = sum(bool(row.get("within_budget")) for row in rows)
    tool_steps = [int(row.get("tool_steps", 0)) for row in rows]
    attempts = [int(row.get("attempts", 0)) for row in rows]
    quality_rows = [row for row in rows if row.get("quality_eligible", True)]
    quality_passed = sum(bool(row.get("quality_passed")) for row in quality_rows)
    prompt_metadata = [
        (row.get("report") if isinstance(row.get("report"), dict) else {}).get("prompt_metadata", {})
        for row in rows
        if isinstance(row.get("report"), dict)
    ]
    prompt_chars = [int(item.get("prompt_chars", 0)) for item in prompt_metadata if item.get("prompt_chars") is not None]
    budget_reduction_runs = sum(bool(item.get("budget_reductions")) for item in prompt_metadata)
    evidence_runs = sum(
        bool((row.get("report") if isinstance(row.get("report"), dict) else {}).get("evidence"))
        for row in rows
    )
    run_errors = [str(row.get("run_error", "")).strip() for row in rows if str(row.get("run_error", "")).strip()]
    return {
        "total_runs": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "quality_total_runs": len(quality_rows),
        "quality_passed": quality_passed,
        "quality_pass_rate": round(quality_passed / len(quality_rows), 4) if quality_rows else 0.0,
        "verifier_passed": verifier_passed,
        "verifier_pass_rate": round(verifier_passed / total, 4) if total else 0.0,
        "within_budget": within_budget,
        "within_budget_rate": round(within_budget / total, 4) if total else 0.0,
        "avg_tool_steps": round(sum(tool_steps) / len(tool_steps), 4) if tool_steps else 0.0,
        "avg_attempts": round(sum(attempts) / len(attempts), 4) if attempts else 0.0,
        # 这些 token 是每次运行最后一次模型请求的运行时记录，不冒充 Provider 的全调用账单。
        "recorded_last_prompt_input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in prompt_metadata),
        "recorded_last_completion_output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in prompt_metadata),
        "avg_last_prompt_chars": round(sum(prompt_chars) / len(prompt_chars), 2) if prompt_chars else 0.0,
        "budget_reduction_runs": budget_reduction_runs,
        "evidence_recorded_runs": evidence_runs,
        "run_error_count": len(run_errors),
        "run_error_samples": sorted(set(run_errors))[:3],
        "failure_categories": _failure_categories(rows),
    }


def _failure_categories(rows):
    """按任务失败分类统计，方便报告定位主要问题。"""
    counts = {}
    for row in rows:
        category = str(row.get("failure_category") or "").strip()
        if category:
            counts[category] = counts.get(category, 0) + 1
    return counts


def run_real_benchmark(
    benchmark_path=DEFAULT_REAL_BENCHMARK,
    artifact_root=DEFAULT_REAL_ARTIFACT_ROOT,
    provider="ollama",
    model=None,
    base_url=None,
    host="http://127.0.0.1:11434",
    temperature=0.0,
    top_p=1.0,
    timeout=300,
    repetitions=2,
    mode="maotuo",
    task_limit=None,
    agent_options=None,
    task_ids=None,
):
    """运行本地真实任务并保存每次重复实验和总汇总。"""
    benchmark_path = Path(benchmark_path)
    artifact_root = Path(artifact_root)
    benchmark = load_benchmark(benchmark_path)
    selected_path = benchmark_path
    temporary_path = None
    if task_limit is not None or task_ids is not None:
        # 子集模式只写临时任务文件，用于 smoke 或自进化 A/B，不改变正式任务文件。
        selected = dict(benchmark)
        if task_ids is None:
            limit = max(1, int(task_limit))
            selected["tasks"] = list(benchmark["tasks"])[:limit]
        else:
            wanted = {str(task_id) for task_id in task_ids}
            selected["tasks"] = [task for task in benchmark["tasks"] if str(task["id"]) in wanted]
            if not selected["tasks"]:
                raise ValueError("task_ids did not select any benchmark task")
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix=".maotuo-real-subset-",
            dir=benchmark_path.parent,
            delete=False,
        )
        try:
            json.dump(selected, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            selected_path = Path(handle.name)
            temporary_path = selected_path
        finally:
            handle.close()
    artifact_root.mkdir(parents=True, exist_ok=True)
    factory = _build_factory(provider, model, base_url, host, temperature, top_p, timeout)
    runtime_options = _mode_options(mode)
    runtime_options.update(dict(agent_options or {}))
    artifacts = []
    for index in range(int(repetitions)):
        artifact_path = artifact_root / f"{mode}-repeat-{index + 1}.json"
        # 使用短目录名，尤其是自进化的 split/baseline/candidate 嵌套路径，避免 Windows 长路径。
        workspace_root = artifact_root / "w" / f"r{index + 1}"
        artifact = run_fixed_benchmark(
            benchmark_path=selected_path,
            artifact_path=artifact_path,
            workspace_root=workspace_root,
            model_name=str(model or provider),
            model_version=str(model or provider),
            temperature=temperature,
            top_p=top_p,
            max_new_tokens=512,
            model_client_factory=factory,
            agent_options=runtime_options,
        )
        artifacts.append(artifact)

    try:
        # GoldenTask 不调用真实模型，随每个组写入结果，专门度量安全发布能力。
        security = run_golden_tasks(feature_flags=runtime_options.get("feature_flags", {}))
        summary = {
            "schema_version": 1,
            "benchmark": str(benchmark_path),
            "task_count": len(selected_tasks := load_benchmark(selected_path)["tasks"]),
            "repetitions": int(repetitions),
            "provider": str(provider),
            "model": str(model or provider),
            "mode": str(mode),
            "feature_flags": dict(runtime_options.get("feature_flags", {})),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "summary": _summary(artifacts),
            "security": security,
            "artifacts": [str(artifact_root / f"{mode}-repeat-{i + 1}.json") for i in range(len(artifacts))],
        }
        (artifact_root / f"{mode}-summary.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return summary
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def run_ablation(
    benchmark_path=DEFAULT_REAL_BENCHMARK,
    artifact_root=DEFAULT_REAL_ARTIFACT_ROOT,
    provider="deepseek",
    model="deepseek-v4-flash",
    base_url=None,
    host="http://127.0.0.1:11434",
    temperature=0.0,
    top_p=1.0,
    timeout=300,
    repetitions=1,
    task_limit=None,
    task_ids=None,
):
    """按固定顺序运行四组消融，确保模型与任务预算完全一致。

    消费者是评测报告与人工复盘；调用者是 CLI 或开发者的评测脚本。
    这个函数不触碰 Agent 核心逻辑，只重复调用已有 ``run_real_benchmark``。
    """
    artifact_root = Path(artifact_root)
    # 四组消融默认固定 Flash；调用方显式传入模型时仍可用于其它受控实验。
    model = model or "deepseek-v4-flash"
    groups = {}
    for mode in ABLATION_MODES:
        groups[mode] = run_real_benchmark(
            benchmark_path=benchmark_path,
            artifact_root=artifact_root,
            provider=provider,
            model=model,
            base_url=base_url,
            host=host,
            temperature=temperature,
            top_p=top_p,
            timeout=timeout,
            repetitions=repetitions,
            mode=mode,
            task_limit=task_limit,
            task_ids=task_ids,
        )
    artifact = {
        "schema_version": 1,
        "provider": str(provider),
        "model": str(model),
        "task_limit": task_limit,
        "task_ids": [str(task_id) for task_id in task_ids] if task_ids is not None else [],
        "repetitions": int(repetitions),
        "modes": list(ABLATION_MODES),
        "groups": groups,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    (artifact_root / "ablation-summary.json").write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return artifact


def rebuild_summary(artifact_root, mode):
    """只用已有 JSON 工件重建汇总，避免为修正统计口径重复消耗模型调用。"""
    artifact_root = Path(artifact_root)
    paths = sorted(artifact_root.glob(f"{mode}-repeat-*.json"))
    artifacts = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not artifacts:
        raise FileNotFoundError(f"no artifacts found for mode: {mode}")
    first = artifacts[0]
    reproducibility = first.get("reproducibility", {})
    summary = {
        "schema_version": 1,
        "benchmark": first.get("benchmark", {}).get("source", ""),
        "task_count": first.get("benchmark", {}).get("task_count", 0),
        "repetitions": len(artifacts),
        "provider": reproducibility.get("model_name", ""),
        "model": reproducibility.get("model_version", ""),
        "mode": str(mode),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "summary": _summary(artifacts),
        "artifacts": [str(path) for path in paths],
    }
    (artifact_root / f"{mode}-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return summary


def build_arg_parser():
    """构造真实评测命令行参数。"""
    parser = argparse.ArgumentParser(description="Run Maotuo local real-model benchmark.")
    parser.add_argument("--benchmark", default=str(DEFAULT_REAL_BENCHMARK))
    parser.add_argument("--artifact-root", default=str(DEFAULT_REAL_ARTIFACT_ROOT))
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="ollama")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--mode", choices=(*ABLATION_MODES, "maotuo"), default="full")
    parser.add_argument("--task-limit", type=int, default=None, help="只运行前 N 个任务，用于真实 Provider smoke 验证。")
    parser.add_argument("--task-id", action="append", default=[], help="指定任务 ID；可重复传入，用于分层抽样。")
    parser.add_argument("--pilot", action="store_true", help="使用固定的分层 8 题小样本。")
    parser.add_argument("--ablation", action="store_true", help="依次运行四组消融实验。")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv=None):
    """执行任务格式校验或真实模型评测。"""
    args = build_arg_parser().parse_args(argv)
    benchmark = load_benchmark(args.benchmark)
    if args.validate_only:
        print(json.dumps({"task_count": len(benchmark["tasks"]), "status": "valid"}, ensure_ascii=False))
        return 0
    if args.pilot and (args.task_limit is not None or args.task_id):
        parser.error("--pilot cannot be combined with --task-limit or --task-id")
    task_ids = STRATIFIED_PILOT_TASK_IDS if args.pilot else (args.task_id or None)
    runner = run_ablation if args.ablation else run_real_benchmark
    result = runner(
        benchmark_path=args.benchmark,
        artifact_root=args.artifact_root,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        host=args.host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.timeout,
        repetitions=args.repetitions,
        task_limit=args.task_limit,
        task_ids=task_ids,
        **({} if args.ablation else {"mode": args.mode}),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
