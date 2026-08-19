"""真实评测报告的格式化回归测试。"""

from maotuo.evaluation.benchmark_report import build_ablation_markdown, build_markdown


def test_build_markdown_keeps_external_error_and_delta():
    """报告必须同时保留指标变化和 Provider 阻断信息。"""
    baseline = {
        "provider": "deepseek",
        "model": "deepseek",
        "summary": {
            "total_runs": 2,
            "pass_rate": 0.0,
            "verifier_pass_rate": 0.5,
            "within_budget_rate": 1.0,
            "avg_tool_steps": 0.0,
            "avg_attempts": 1.0,
            "avg_last_prompt_chars": 0.0,
            "budget_reduction_runs": 0,
            "evidence_recorded_runs": 0,
            "run_error_count": 2,
            "run_error_samples": ["HTTP 402"],
            "failure_categories": {"model_error": 2},
        },
    }
    maotuo = {"provider": "deepseek", "model": "deepseek", "summary": baseline["summary"]}
    report = build_markdown(baseline, maotuo)
    assert "外部阻断" in report
    assert "HTTP 402" in report
    assert "任务通过率" in report


def test_build_ablation_markdown_shows_incremental_and_security_metrics():
    """四组报告必须把上下文收益与 GoldenTask 安全收益分开呈现。"""
    groups = {}
    for name, rate, passed in (
        ("baseline", 0.4, 3),
        ("prompt_only", 0.5, 3),
        ("prompt_context", 0.6, 3),
        ("full", 0.62, 4),
    ):
        groups[name] = {
            "summary": {key: 0 for key, _, _ in ()},
            "security": {"passed": passed, "total": 4},
        }
        groups[name]["summary"].update(
            {
                "pass_rate": rate,
                "quality_pass_rate": rate,
                "verifier_pass_rate": rate,
                "within_budget_rate": 1.0,
                "avg_attempts": 4.0,
                "avg_last_prompt_chars": 1000,
                "budget_reduction_runs": 1,
                "evidence_recorded_runs": 1,
                "run_error_count": 0,
            }
        )
    report = build_ablation_markdown({"provider": "deepseek", "model": "deepseek-v4-flash", "groups": groups})

    assert "Prompt + Context" in report
    assert "DeepSeek" in report
    assert "deepseek-v4-flash" not in report
    assert "+10.00 pp" in report
    assert "| Full | 4 | 4 |" in report
