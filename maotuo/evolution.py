"""受控自进化信号收集。

本模块只记录失败信号和提示词改进候选，不会自动修改工具白名单、
安全规则或当前运行配置；候选必须经过外部评测和人工审批后才能生效。
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


@dataclass
class EvolutionSignalCollector:
    """收集 stale 和任务失败信号，供离线评测流程消费。"""

    signals: list[dict] = field(default_factory=list)

    def record(self, kind, payload=None):
        """追加一个可审计的信号，并返回该信号。"""
        signal = {"kind": str(kind), "payload": dict(payload or {})}
        self.signals.append(signal)
        return signal

    def candidate(self, signal):
        """根据有限信号生成提示策略候选，初始状态固定为待审批。"""
        kind = signal.get("kind", "")
        if kind == "stale_retry_exhausted":
            proposal = "回答前必须重新读取所有 stale 文件，并基于最新内容作答。"
        elif kind == "task_failed":
            proposal = "任务失败时先说明失败原因、已验证证据和下一步动作。"
        else:
            proposal = "在回答前复核本轮工具证据和最终结论的一致性。"
        return {
            "source": kind,
            "proposal": proposal,
            "scope": "prompt_strategy",
            "status": "pending_approval",
        }


class EvolutionStore:
    """持久化候选提示策略，并提供人工审批后的版本化激活。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "candidates.json"

    def _load(self):
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return list(payload) if isinstance(payload, list) else []

    def _save(self, candidates):
        """使用同目录临时文件原子替换，避免进程中断留下半份候选数据。"""
        payload = json.dumps(candidates, indent=2, ensure_ascii=False) + "\n"
        temporary_path = None
        try:
            # 临时文件和目标文件放在同一目录，才能保证 replace 具备原子性。
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".candidates-",
                suffix=".tmp",
                dir=self.root,
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    def _evaluate_candidate(self):
        """自动运行 GoldenTask 和 ReplayCase，生成审批依据。"""
        evaluated_at = datetime.now(timezone.utc).isoformat()
        try:
            # 使用局部导入避免 evolution 与 runtime/evaluation 形成循环依赖。
            from .evaluation.replay import run_golden_tasks, run_replay_cases

            golden = run_golden_tasks()
            replay = run_replay_cases(
                cases_path=self.root.parent / "evaluation" / "replay_cases.json"
            )
            total = int(golden.get("total", 0)) + int(replay.get("total", 0))
            passed = int(golden.get("passed", 0)) + int(replay.get("passed", 0))
            return {
                "status": "passed" if total and passed == total else "failed",
                "approval_ready": bool(total and passed == total),
                "total": total,
                "passed": passed,
                "pass_rate": round(passed / total, 4) if total else 0.0,
                "evaluated_at": evaluated_at,
                "golden": golden,
                "replay": replay,
            }
        except Exception as exc:
            # 评测本身失败时，候选仍然保留，但明确禁止把它当作已验证策略。
            return {
                "status": "error",
                "approval_ready": False,
                "total": 0,
                "passed": 0,
                "pass_rate": 0.0,
                "evaluated_at": evaluated_at,
                "error": str(exc),
            }

    def create_candidate(self, candidate, signal):
        """保存候选，并自动附加 Golden/Replay 评测结果。"""
        item = dict(candidate)
        item.update(
            {
                "candidate_id": "prompt-" + uuid4().hex[:10],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "signal": dict(signal),
                "version": 1,
            }
        )
        # 评测结果跟候选一起持久化，审批人不需要依赖另一份临时输出判断。
        item["evaluation"] = self._evaluate_candidate()
        candidates = self._load()
        candidates.append(item)
        self._save(candidates)
        return item

    def list(self, status=""):
        """列出候选，默认返回全部。"""
        items = self._load()
        return [item for item in items if not status or item.get("status") == status]

    def approve(self, candidate_id):
        """仅允许评测通过的候选进入 active prompt strategy。"""
        candidates = self._load()
        target = None
        for item in candidates:
            if item.get("candidate_id") == str(candidate_id):
                # 缺失评测结果也按失败处理，保证旧数据不会绕过新审批门槛。
                if item.get("evaluation", {}).get("approval_ready") is not True:
                    raise ValueError("candidate has not passed evaluation")
                item["status"] = "approved"
                item["approved_at"] = datetime.now(timezone.utc).isoformat()
                target = item
            elif item.get("status") == "active":
                item["status"] = "approved"
        if target is None:
            raise ValueError(f"unknown evolution candidate: {candidate_id}")
        target["status"] = "active"
        self._save(candidates)
        return target

    def active_strategy(self):
        """返回当前生效的提示策略，永远不返回安全策略或工具权限。"""
        active = [item for item in self._load() if item.get("status") == "active"]
        return active[-1] if active else None
