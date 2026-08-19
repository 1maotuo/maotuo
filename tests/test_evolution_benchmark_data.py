"""受控自进化评测数据的划分和门槛回归测试。"""

import json
from pathlib import Path

from maotuo.evaluation.evolution_benchmark import _acceptance, _load_cases, _split_task_ids


def test_evolution_cases_have_train_holdout_split():
    """自进化数据必须把候选生成和泛化验证分开。"""
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "maotuo_evolution_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) == 8
    assert sum(item["split"] == "train" for item in cases) == 4
    assert sum(item["split"] == "holdout" for item in cases) == 4
    assert payload["acceptance"]["failed_candidate_must_not_activate"] is True


def test_evolution_cases_map_to_real_tasks_and_acceptance_is_fail_closed():
    """每个案例都必须落到真实任务，门槛未满足时不能进入审批就绪。"""
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "maotuo_evolution_cases.json"
    _, cases = _load_cases(path)
    assert len(_split_task_ids(cases, "train")) == 4
    assert len(_split_task_ids(cases, "holdout")) == 4
    before = {"summary": {"pass_rate": 0.5}}
    after = {"summary": {"pass_rate": 0.5}}
    golden = {"total": 4, "passed": 3}
    result = _acceptance(before, after, before, after, golden)
    assert result["approval_ready"] is False
