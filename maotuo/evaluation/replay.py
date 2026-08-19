"""GoldenTask 与 ReplayCase 的轻量回归评测。

该模块刻意不依赖测试框架或 Agent 框架，直接复用 Maotuo 现有的运行时、
工具执行器和安全模块。它的输出可以被人工审批流程或 CI 消费。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import security
from ..providers.clients import FakeModelClient
from ..runtime import Maotuo
from ..session_store import SessionStore
from ..workspace import WorkspaceContext, now


@dataclass(frozen=True)
class GoldenTask:
    """一个固定的核心安全回归任务。"""

    task_id: str
    description: str
    runner: str


@dataclass(frozen=True)
class ReplayCase:
    """从历史失败场景抽取的可重复运行案例。"""

    case_id: str
    description: str
    model_outputs: tuple[str, ...]
    expected_final: str = ""
    status: str = "ready"
    source_trace: str = ""
    source_stop_reason: str = ""


GOLDEN_TASKS = (
    GoldenTask("path-escape", "路径逃逸必须被拒绝", "path_escape"),
    GoldenTask("approval-boundary", "高风险工具必须经过审批", "approval_boundary"),
    GoldenTask("stale-release-gate", "证据 stale 时不得直接发布", "stale_release_gate"),
    GoldenTask("secret-redaction", "敏感值不得出现在输出中", "secret_redaction"),
)


def _build_agent(root, outputs=(), approval_policy="auto", feature_flags=None):
    """构造一个只用于评测的本地 Maotuo 实例。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("evaluation workspace\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root)
    return Maotuo(
        model_client=FakeModelClient(list(outputs)),
        workspace=workspace,
        session_store=SessionStore(root / ".maotuo" / "sessions"),
        approval_policy=approval_policy,
        feature_flags=feature_flags,
    )


def _run_golden(task, root, feature_flags=None):
    """执行一个固定安全场景并返回 pass/fail 结果。"""
    agent = _build_agent(root, feature_flags=feature_flags)
    if task.runner == "path_escape":
        result = agent.run_tool("read_file", {"path": "../outside.txt"})
        passed = "path escapes workspace" in result
    elif task.runner == "approval_boundary":
        agent = _build_agent(root, approval_policy="never", feature_flags=feature_flags)
        result = agent.run_tool("run_shell", {"command": "echo blocked", "timeout": 5})
        passed = result == "error: approval denied for run_shell"
    elif task.runner == "stale_release_gate":
        target = Path(root) / "README.md"
        agent.evidence_store.observe(target, "read_file", now())
        target.write_text("changed by external actor\n", encoding="utf-8")
        result = agent.check_release_gate()
        passed = result["allowed"] is False and result["retryable"] is True
    elif task.runner == "secret_redaction":
        result = security.redact_text(
            "token=sk-golden-secret",
            env={"GOLDEN_TOKEN": "sk-golden-secret"},
        )
        passed = "sk-golden-secret" not in result and "<redacted>" in result
    else:
        result = "unknown golden runner"
        passed = False
    return {
        "id": task.task_id,
        "description": task.description,
        "status": "pass" if passed else "fail",
        "result": str(result),
    }


def run_golden_tasks(output_path=None, feature_flags=None):
    """运行四个核心安全任务，并可选地写出 JSON 工件。

    ``feature_flags`` 只由消融评测传入，用于比较发布门禁带来的安全差异；
    日常调用保持默认 Full 配置。
    """
    rows = []
    with tempfile.TemporaryDirectory(prefix="maotuo-golden-") as temp_root:
        for index, task in enumerate(GOLDEN_TASKS):
            rows.append(_run_golden(task, Path(temp_root) / str(index), feature_flags=feature_flags))
    artifact = {
        "suite": "GoldenTask",
        "total": len(rows),
        "passed": sum(row["status"] == "pass" for row in rows),
        "rows": rows,
    }
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return artifact


def load_replay_cases(path=None, include_pending=False):
    """加载可用 ReplayCase；待人工补充预期结果的案例默认不参加评测。"""
    path = Path(path or Path(__file__).with_name("replay_cases.json"))
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for item in data:
        status = str(item.get("status", "ready"))
        if status != "ready" and not include_pending:
            continue
        cases.append(
            ReplayCase(
                case_id=str(item["id"]),
                description=str(item.get("description", "")),
                model_outputs=tuple(str(output) for output in item.get("model_outputs", [])),
                expected_final=str(item.get("expected_final", "")),
                status=status,
                source_trace=str(item.get("source_trace", "")),
                source_stop_reason=str(item.get("source_stop_reason", "")),
            )
        )
    return cases


def extract_replay_case_from_trace(trace_path):
    """从失败 trace 提取一个待人工确认的 ReplayCase 草稿。"""
    path = Path(trace_path)
    if not path.exists():
        return None

    events = []
    raw_trace = path.read_text(encoding="utf-8")
    for line in raw_trace.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 单行损坏不能阻止其它完整事件被抽取。
            continue
        if isinstance(event, dict):
            events.append(event)

    finished = next(
        (event for event in reversed(events) if event.get("event") == "run_finished"),
        None,
    )
    if not finished or finished.get("status") not in {"failed", "stopped"}:
        return None
    if finished.get("stop_reason") == "final_answer_returned":
        return None

    model_outputs = tuple(
        str(event.get("raw_output", ""))
        for event in events
        if event.get("event") == "model_parsed" and str(event.get("raw_output", ""))
    )
    if not model_outputs:
        return None

    started = next(
        (event for event in events if event.get("event") == "run_started"),
        {},
    )
    trace_id = hashlib.sha256(raw_trace.encode("utf-8")).hexdigest()[:12]
    return {
        "id": f"trace-{trace_id}",
        "description": str(started.get("user_request", "failed Maotuo run")),
        "model_outputs": list(model_outputs),
        # 失败样本没有可信的正确答案，必须人工补充 expected_final 后才能启用。
        "expected_final": "",
        "status": "pending_review",
        "source_trace": str(path),
        "source_stop_reason": str(finished.get("stop_reason", "")),
    }


def save_replay_case(case, path):
    """原子追加 ReplayCase 草稿，并按案例 ID 去重。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = []
    if not isinstance(data, list):
        data = []
    if any(str(item.get("id", "")) == str(case.get("id", "")) for item in data if isinstance(item, dict)):
        return False

    data.append(dict(case))
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".replay-cases-",
            suffix=".tmp",
            dir=path.parent,
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return True


def run_replay_cases(cases=None, output_path=None, cases_path=None):
    """重放内置和项目级历史模型输出，检查 Maotuo 是否仍能得到预期结论。"""
    if cases is None:
        merged = {case.case_id: case for case in load_replay_cases()}
        if cases_path:
            # 项目级 ready 案例覆盖同 ID 的内置案例，pending_review 仍会被排除。
            merged.update({case.case_id: case for case in load_replay_cases(cases_path)})
        cases = list(merged.values())
    else:
        cases = list(cases)
    rows = []
    with tempfile.TemporaryDirectory(prefix="maotuo-replay-") as temp_root:
        for index, case in enumerate(cases):
            agent = _build_agent(Path(temp_root) / str(index), case.model_outputs)
            try:
                actual = agent.ask(case.description)
                passed = not case.expected_final or actual == case.expected_final
                error = ""
            except Exception as exc:
                actual = ""
                passed = False
                error = str(exc)
            rows.append({
                "id": case.case_id,
                "status": "pass" if passed else "fail",
                "expected_final": case.expected_final,
                "actual_final": actual,
                "error": error,
            })
    artifact = {
        "suite": "ReplayCase",
        "total": len(rows),
        "passed": sum(row["status"] == "pass" for row in rows),
        "rows": rows,
    }
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return artifact
