"""真实评测结果的对照报告生成器。

本模块消费者是 README、简历数据和人工复盘；调用方是评测完成后的命令行。
它只读取 JSON 汇总，不修改运行工件和 Agent 核心逻辑。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


METRICS = (
    ("pass_rate", "任务通过率", "{:.2%}"),
    ("quality_pass_rate", "有效任务通过率", "{:.2%}"),
    ("verifier_pass_rate", "Verifier 通过率", "{:.2%}"),
    ("within_budget_rate", "预算内完成率", "{:.2%}"),
    ("avg_attempts", "平均模型轮次", "{:.2f}"),
    ("avg_last_prompt_chars", "末轮 Prompt 平均字符数", "{:.2f}"),
    ("budget_reduction_runs", "发生上下文裁剪的运行数", "{}"),
        ("evidence_recorded_runs", "写入证据的运行数", "{}"),
        ("run_error_count", "模型/Provider 异常运行数", "{}"),
)


def _load(path):
    """读取一个 real_benchmark 汇总文件。"""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _format(value, pattern):
    """统一格式化百分比、耗时和计数。"""
    return pattern.format(value)


def _display_model(summary):
    """对外报告使用供应商名称，精确模型仍保留在原始 JSON 工件中。"""
    provider = str(summary.get("provider", "")).strip().lower()
    if provider == "deepseek":
        return "DeepSeek"
    return str(summary.get("model") or summary.get("provider") or "unknown")


def build_markdown(baseline, maotuo):
    """生成可直接放入项目文档的 Baseline/Maotuo 对照表。"""
    before = baseline.get("summary", {})
    after = maotuo.get("summary", {})
    lines = [
        "# Maotuo Real Benchmark Report",
        "",
        "> 数据来自真实模型调用；成功必须同时满足 verifier、步数预算、验收文件和正常结束四项条件。",
        "",
        f"- Model provider: `{_display_model(maotuo)}`",
        f"- Task runs: `{after.get('total_runs', 0)}`",
        "",
        "| 指标 | Baseline | Maotuo | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for key, label, pattern in METRICS:
        old = before.get(key, 0)
        new = after.get(key, 0)
        delta = new - old
        lines.append(f"| {label} | {_format(old, pattern)} | {_format(new, pattern)} | {_format(delta, pattern)} |")
    lines.extend(
        [
            "",
            "## 失败分类",
            "",
            f"- Baseline: `{json.dumps(before.get('failure_categories', {}), ensure_ascii=False)}`",
            f"- Maotuo: `{json.dumps(after.get('failure_categories', {}), ensure_ascii=False)}`",
            "",
            "## 外部阻断",
            "",
            f"- Baseline errors: `{json.dumps(before.get('run_error_samples', []), ensure_ascii=False)}`",
            f"- Maotuo errors: `{json.dumps(after.get('run_error_samples', []), ensure_ascii=False)}`",
            "",
            "## 口径说明",
            "",
            "`recorded_last_*_tokens` 只统计每次运行最后一次模型请求中被 Runtime 记录的 token，不能替代 Provider 账单；如需成本，必须绑定实际价格表后再计算。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_ablation_markdown(ablation):
    """生成四组消融报告，分别展示 Prompt、上下文和发布门禁的贡献。"""
    groups = dict(ablation.get("groups", {}) or {})
    labels = {
        "baseline": "Baseline",
        "prompt_only": "Prompt Only",
        "prompt_context": "Prompt + Context",
        "full": "Full",
    }
    ordered = [mode for mode in labels if mode in groups]
    if not ordered:
        raise ValueError("ablation report has no groups")
    lines = [
        "# Maotuo Ablation Report",
        "",
        "> 四组使用同一任务、同一模型配置、同一温度和同一预算。基础路径校验、审批和脱敏始终开启。",
        "",
        f"- Model provider: `{_display_model(ablation)}`",
        f"- Modes: `{', '.join(labels[mode] for mode in ordered)}`",
        "",
        "| 指标 | " + " | ".join(labels[mode] for mode in ordered) + " |",
        "|---|" + "|".join("---:" for _ in ordered) + "|",
    ]
    for key, label, pattern in METRICS:
        values = [dict(groups[mode].get("summary", {})).get(key, 0) for mode in ordered]
        lines.append("| " + label + " | " + " | ".join(_format(value, pattern) for value in values) + " |")

    lines.extend(["", "## 关键增量", ""])
    prompt_only = dict(groups.get("prompt_only", {}).get("summary", {})).get("pass_rate", 0)
    prompt_context = dict(groups.get("prompt_context", {}).get("summary", {})).get("pass_rate", 0)
    full = dict(groups.get("full", {}).get("summary", {})).get("pass_rate", 0)
    lines.extend(
        [
            f"- 高价值上下文相对 Prompt Only：`{(prompt_context - prompt_only) * 100:+.2f} pp`。",
            f"- Full 相对 Prompt Only：`{(full - prompt_only) * 100:+.2f} pp`。",
            f"- Full 相对 Prompt + Context：`{(full - prompt_context) * 100:+.2f} pp`。",
            "",
            "## 安全 GoldenTask",
            "",
            "| 组别 | 通过数 | 总数 |",
            "|---|---:|---:|",
        ]
    )
    for mode in ordered:
        security = dict(groups[mode].get("security", {}))
        lines.append(f"| {labels[mode]} | {security.get('passed', 0)} | {security.get('total', 0)} |")
    lines.extend(
        [
            "",
            "## 口径说明",
            "",
            "功能通过率来自真实 Coding 任务 verifier；GoldenTask 单独衡量安全机制。原始 JSON 保留精确模型与特性开关，对外文档仅标记模型供应商。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_report(baseline_path, maotuo_path, output_path):
    """读取两组汇总并写出 Markdown 报告。"""
    text = build_markdown(_load(baseline_path), _load(maotuo_path))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return text


def write_ablation_report(ablation_path, output_path):
    """读取四组消融 JSON，并写出对外可展示的 Markdown 报告。"""
    text = build_ablation_markdown(_load(ablation_path))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return text


def build_arg_parser():
    """构造报告命令行参数。"""
    parser = argparse.ArgumentParser(description="Compare Maotuo real benchmark summaries.")
    parser.add_argument("--baseline")
    parser.add_argument("--maotuo")
    parser.add_argument("--output", required=True)
    parser.add_argument("--ablation", default=None, help="四组消融汇总 JSON；提供后忽略 baseline/maotuo。")
    return parser


def main(argv=None):
    """执行报告生成。"""
    args = build_arg_parser().parse_args(argv)
    if args.ablation:
        print(write_ablation_report(args.ablation, args.output), end="")
    else:
        if not args.baseline or not args.maotuo:
            parser.error("--baseline and --maotuo are required unless --ablation is provided")
        print(write_report(args.baseline, args.maotuo, args.output), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
