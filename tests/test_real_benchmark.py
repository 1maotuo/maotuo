"""本地真实任务集的格式、工作区和指标汇总回归测试。"""

import json
import subprocess
import sys
from pathlib import Path

from maotuo.evaluation.evaluator import load_benchmark
from maotuo.evaluation.real_benchmark import ABLATION_MODES, STRATIFIED_PILOT_TASK_IDS, _mode_options, _summary, run_ablation


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "benchmarks" / "maotuo_real_tasks.json"
FIXTURE = ROOT / "benchmarks" / "fixtures" / "maotuo_taskboard"


def test_real_task_manifest_has_24_tasks_and_expected_categories():
    """任务集必须是可加载、可复现的 24 个本地代码任务。"""
    benchmark = load_benchmark(TASKS, repo_root=ROOT)
    assert len(benchmark["tasks"]) == 24
    assert len({task["id"] for task in benchmark["tasks"]}) == 24
    assert {task["category"] for task in benchmark["tasks"]} == {
        "code-repair",
        "test-repair",
        "controlled-edit",
        "security-review",
        "recovery",
    }


def test_local_taskboard_baseline_tests_pass():
    """本地工作区在 Agent 修改前也应具备可运行的基础测试。"""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=FIXTURE,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ablation_modes_have_clear_incremental_boundaries():
    """四组消融只能逐层增加 Prompt、上下文和发布门禁能力。"""
    flags = _mode_options("baseline")["feature_flags"]
    assert flags == {
        "memory": False,
        "relevant_memory": False,
        "context_reduction": False,
        "prompt_cache": False,
        "coding_prompt": False,
        "high_value_context": False,
        "evidence_chain": False,
        "release_gate": False,
    }
    assert ABLATION_MODES == ("baseline", "prompt_only", "prompt_context", "full")
    assert len(STRATIFIED_PILOT_TASK_IDS) == 8
    assert _mode_options("prompt_only")["feature_flags"]["coding_prompt"] is True
    assert _mode_options("prompt_only")["feature_flags"]["high_value_context"] is False
    assert _mode_options("prompt_context")["feature_flags"]["high_value_context"] is True
    assert _mode_options("prompt_context")["feature_flags"]["release_gate"] is False
    assert _mode_options("full")["feature_flags"] == {}


def test_summary_aggregates_real_run_rows():
    """多次真实运行应能汇总成简历可解释的统计口径。"""
    summary = _summary(
        [
            {"rows": [{"passed": True, "verifier_passed": True, "within_budget": True, "tool_steps": 2, "attempts": 3}]},
            {"rows": [{"passed": False, "verifier_passed": False, "within_budget": True, "tool_steps": 4, "attempts": 5, "failure_category": "verifier_failed"}]},
        ]
    )
    assert summary["total_runs"] == 2
    assert summary["passed"] == 1
    assert summary["verifier_pass_rate"] == 0.5
    assert summary["failure_categories"] == {"verifier_failed": 1}


def test_summary_exposes_quality_rate_without_counting_prepassed_tasks():
    """有效任务通过率应排除开始前已通过 Verifier 的不可评分任务。"""
    summary = _summary(
        [
            {
                "rows": [
                    {"passed": True, "quality_eligible": False, "quality_passed": False},
                    {"passed": True, "quality_eligible": True, "quality_passed": True},
                ]
            }
        ]
    )
    assert summary["passed"] == 2
    assert summary["quality_total_runs"] == 1
    assert summary["quality_passed"] == 1
    assert summary["quality_pass_rate"] == 1.0


def test_run_ablation_uses_all_four_modes_without_rebuilding_task_manifest(monkeypatch, tmp_path):
    """消融入口应按固定四组调用已有真实评测函数，并持久化总汇总。"""
    calls = []

    def fake_run_real_benchmark(**kwargs):
        calls.append(kwargs["mode"])
        return {
            "mode": kwargs["mode"],
            "provider": kwargs["provider"],
            "model": kwargs["model"],
            "summary": {"pass_rate": 0.5},
            "security": {"passed": 4, "total": 4},
        }

    monkeypatch.setattr("maotuo.evaluation.real_benchmark.run_real_benchmark", fake_run_real_benchmark)
    artifact = run_ablation(artifact_root=tmp_path, task_ids=("core-no-input-mutation",))

    assert calls == list(ABLATION_MODES)
    assert artifact["model"] == "deepseek-v4-flash"
    assert artifact["task_ids"] == ["core-no-input-mutation"]
    assert (tmp_path / "ablation-summary.json").is_file()


def test_real_task_manifest_is_json_object():
    """任务文件本身保持可审计的 JSON 结构，便于离线重建。"""
    payload = json.loads(TASKS.read_text(encoding="utf-8"))
    assert payload["name"] == "maotuo-local-real-v1"
    assert payload["tasks"]
