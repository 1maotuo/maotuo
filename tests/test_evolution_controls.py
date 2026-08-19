"""自进化、Role 过滤、ReleaseGate 和受保护 Prompt 的回归测试。"""

import json

import pytest

from maotuo.evidence import EvidenceStore, ReleaseGate
from maotuo.evaluation.replay import (
    extract_replay_case_from_trace,
    load_replay_cases,
    run_golden_tasks,
    save_replay_case,
)
from maotuo.evolution import EvolutionStore
from maotuo.role_skill import Role, RoleSkillManager, Skill


def test_candidate_contains_automatic_evaluation(monkeypatch, tmp_path):
    """候选生成后必须同步保存 GoldenTask/ReplayCase 评测摘要。"""
    golden = {"suite": "GoldenTask", "total": 4, "passed": 4, "rows": []}
    replay = {"suite": "ReplayCase", "total": 2, "passed": 1, "rows": []}

    # 用固定结果替代真实评测，单测只验证候选与评测结果的连接关系。
    monkeypatch.setattr("maotuo.evaluation.replay.run_golden_tasks", lambda: golden)
    monkeypatch.setattr("maotuo.evaluation.replay.run_replay_cases", lambda **_: replay)

    store = EvolutionStore(tmp_path / "evolution")
    candidate = store.create_candidate(
        {"source": "task_failed", "proposal": "复核证据", "status": "pending_approval"},
        {"kind": "task_failed", "payload": {"reason": "test"}},
    )

    assert candidate["evaluation"]["total"] == 6
    assert candidate["evaluation"]["passed"] == 5
    assert candidate["evaluation"]["pass_rate"] == 0.8333
    assert candidate["evaluation"]["approval_ready"] is False
    assert store.list()[0]["evaluation"]["golden"]["suite"] == "GoldenTask"


def test_evidence_store_isolated_between_runs(tmp_path):
    """新 Run 开始后，上一轮文件证据和 stale 状态都必须清空。"""
    target = tmp_path / "README.md"
    target.write_text("first\n", encoding="utf-8")
    store = EvidenceStore(tmp_path)

    store.start_run("run-1")
    store.observe(target, "read_file", "now")
    target.write_text("changed outside run\n", encoding="utf-8")
    assert store.stale() == ["README.md"]

    store.start_run("run-2")
    assert store.run_id == "run-2"
    assert store.to_dict() == {}
    assert store.stale() == []


def test_maotuo_ask_starts_fresh_evidence_run(tmp_path):
    """连续调用同一个 Maotuo 实例时，第二次 ask 不应继承第一次的证据。"""
    from maotuo.providers.clients import FakeModelClient
    from maotuo.runtime import Maotuo
    from maotuo.session_store import SessionStore
    from maotuo.workspace import WorkspaceContext

    (tmp_path / "README.md").write_text("run evidence\n", encoding="utf-8")
    agent = Maotuo(
        model_client=FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":10}}</tool>',
                "<final>first</final>",
                "<final>second</final>",
            ]
        ),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".maotuo" / "sessions"),
    )

    assert agent.ask("read once") == "first"
    assert agent.evidence_store.to_dict()
    assert agent.ask("do not read") == "second"
    assert agent.evidence_store.run_id
    assert agent.evidence_store.to_dict() == {}


def test_failed_candidate_cannot_be_approved(monkeypatch, tmp_path):
    """评测失败或评测异常的候选必须 fail-closed。"""
    failed = {"suite": "GoldenTask", "total": 1, "passed": 0, "rows": []}
    replay = {"suite": "ReplayCase", "total": 1, "passed": 1, "rows": []}
    monkeypatch.setattr("maotuo.evaluation.replay.run_golden_tasks", lambda: failed)
    monkeypatch.setattr("maotuo.evaluation.replay.run_replay_cases", lambda: replay)

    store = EvolutionStore(tmp_path / "evolution")
    candidate = store.create_candidate(
        {"source": "task_failed", "proposal": "不要激活", "status": "pending_approval"},
        {"kind": "task_failed", "payload": {}},
    )

    with pytest.raises(ValueError, match="candidate has not passed evaluation"):
        store.approve(candidate["candidate_id"])


def test_evolution_store_writes_valid_json_atomically(monkeypatch, tmp_path):
    """候选持久化后必须是完整可解析的 JSON 文件。"""
    passed = {"suite": "GoldenTask", "total": 1, "passed": 1, "rows": []}
    monkeypatch.setattr("maotuo.evaluation.replay.run_golden_tasks", lambda: passed)
    monkeypatch.setattr("maotuo.evaluation.replay.run_replay_cases", lambda: passed)

    store = EvolutionStore(tmp_path / "evolution")
    store.create_candidate(
        {"source": "task_failed", "proposal": "安全策略", "status": "pending_approval"},
        {"kind": "task_failed", "payload": {}},
    )

    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert not list(store.root.glob(".candidates-*.tmp"))


def test_failed_trace_can_seed_pending_replay_case(tmp_path):
    """失败 trace 应能生成待人工补充期望答案的 ReplayCase 草稿。"""
    trace = tmp_path / "trace.jsonl"
    events = [
        {"event": "run_started", "user_request": "修复安全检查"},
        {"event": "model_parsed", "raw_output": "<tool>{bad json}</tool>"},
        {"event": "model_parsed", "raw_output": "仍然无法回答"},
        {
            "event": "run_finished",
            "status": "failed",
            "stop_reason": "retry_limit_reached",
            "final_answer": "任务失败",
        },
    ]
    trace.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8")

    case = extract_replay_case_from_trace(trace)
    assert case["status"] == "pending_review"
    assert case["description"] == "修复安全检查"
    assert case["model_outputs"] == ["<tool>{bad json}</tool>", "仍然无法回答"]

    cases_path = tmp_path / ".maotuo" / "evaluation" / "replay_cases.json"
    assert save_replay_case(case, cases_path) is True
    assert save_replay_case(case, cases_path) is False
    assert load_replay_cases(cases_path) == []
    assert len(load_replay_cases(cases_path, include_pending=True)) == 1

    # 人工补齐期望结果并标记 ready 后，项目级案例才能进入正式 Replay 评测。
    ready = dict(case)
    ready["status"] = "ready"
    ready["expected_final"] = "修复完成"
    cases_path.write_text(json.dumps([ready], ensure_ascii=False), encoding="utf-8")
    assert len(load_replay_cases(cases_path)) == 1


def test_read_only_role_filters_dangerous_guidance():
    """只读角色不应收到写文件、补丁或命令执行提示。"""
    manager = RoleSkillManager(
        role=Role(name="reviewer", allowed_tools=("list_files", "read_file", "search")),
    )

    assert manager.is_read_only() is True
    assert manager.filter_guidance("先使用 write_file 修改文件") == ""
    assert manager.filter_guidance("先核对证据再回答") == "先核对证据再回答"


def test_release_gate_reset_isolated_between_runs(tmp_path):
    """ReleaseGate 清零后，下一次运行可以重新使用完整重试次数。"""
    gate = ReleaseGate(object(), max_retries=2)
    gate.retry_count = 2
    gate.reset()

    assert gate.retry_count == 0


def test_golden_task_exposes_release_gate_contribution():
    """关闭发布门禁的对照组只能失去 stale 场景，其他基础安全项仍需通过。"""
    baseline = run_golden_tasks(
        feature_flags={
            "evidence_chain": False,
            "release_gate": False,
        }
    )
    full = run_golden_tasks()

    assert baseline["passed"] == 3
    assert full["passed"] == 4
    assert next(row for row in baseline["rows"] if row["id"] == "stale-release-gate")["status"] == "fail"


def test_role_and_skill_prompt_stay_at_prefix_head_when_budget_is_small(tmp_path):
    """上下文预算很小时，Role/Skill 硬约束仍必须完整出现在 Prompt 中。"""
    from maotuo.providers.clients import FakeModelClient
    from maotuo.runtime import Maotuo
    from maotuo.session_store import SessionStore
    from maotuo.workspace import WorkspaceContext
    from maotuo.context_manager import ContextManager

    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = Maotuo(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".maotuo" / "sessions"),
        role=Role(
            name="reviewer",
            base_prompt="ROLE-HARD-CONSTRAINT",
            allowed_tools=("list_files", "read_file", "search"),
        ),
        skills=[Skill("security", keywords=("安全",), prompt_fragment="SKILL-HARD-CONSTRAINT")],
    )

    prompt, _ = ContextManager(
        agent,
        total_budget=120,
        section_budgets={"prefix": 20, "memory": 20, "relevant_memory": 20, "history": 20},
    ).build("安全")

    assert "ROLE-HARD-CONSTRAINT" in prompt
    assert "SKILL-HARD-CONSTRAINT" in prompt
    assert prompt.startswith("Protected Role/Skill policy:")
