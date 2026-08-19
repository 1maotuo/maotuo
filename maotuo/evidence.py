"""文件证据链与最终答案发布闸门。

这个模块只负责回答两个问题：模型依据的文件现在是否仍然是最新的，
以及最终答案是否可以安全发布。它不负责工具权限，也不修改现有安全规则。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileEvidence:
    """一次文件观察结果。"""

    path: str
    sha256: str
    source: str
    observed_at: str


@dataclass
class EvidenceStore:
    """保存当前运行中已经观察过的文件证据。"""

    root: Path
    run_id: str = ""
    items: dict[str, FileEvidence] = field(default_factory=dict)

    def start_run(self, run_id):
        """切换到新的 Run，并清空上一个 Run 的文件证据。"""
        self.run_id = str(run_id or "")
        self.items.clear()

    def _relative(self, path):
        """把路径统一成工作区相对路径，避免同一文件出现多个 key。"""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            return candidate.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return ""

    def observe(self, path, source, observed_at):
        """记录一个文件当前的 SHA256；文件不存在时不产生伪证据。"""
        relative = self._relative(path)
        if not relative:
            return None
        absolute = self.root / relative
        if not absolute.is_file():
            return None
        try:
            digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        except OSError:
            return None
        evidence = FileEvidence(relative, digest, str(source), str(observed_at))
        self.items[relative] = evidence
        return evidence

    def observe_tool(self, name, args, metadata, observed_at):
        """根据工具参数和执行结果收集本次工具涉及的文件。"""
        paths = list(metadata.get("affected_paths", []) or [])
        explicit_path = args.get("path") if isinstance(args, dict) else None
        if name in {"read_file", "write_file", "patch_file"} and explicit_path:
            paths.append(explicit_path)
        for path in dict.fromkeys(str(item) for item in paths if str(item).strip()):
            self.observe(path, name, observed_at)

    def stale(self):
        """返回读取后发生变化、删除或无法读取的文件。"""
        stale_paths = []
        for relative, evidence in self.items.items():
            path = self.root / relative
            if not path.is_file():
                stale_paths.append(relative)
                continue
            try:
                current = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                stale_paths.append(relative)
                continue
            if current != evidence.sha256:
                stale_paths.append(relative)
        return sorted(set(stale_paths))

    def to_dict(self):
        """转换为可写入 trace 和 report 的普通字典。"""
        return {
            path: {
                "sha256": item.sha256,
                "source": item.source,
                "observed_at": item.observed_at,
            }
            for path, item in sorted(self.items.items())
        }


class ReleaseGate:
    """在 final 返回给用户前执行证据新鲜度检查。"""

    def __init__(self, evidence_store, max_retries=2):
        self.evidence_store = evidence_store
        self.max_retries = int(max_retries)
        self.retry_count = 0

    def reset(self):
        """开始新的 ask() 前清零重试次数，避免不同运行相互污染。"""
        self.retry_count = 0

    def check(self):
        """返回是否允许发布，以及 stale 文件和当前重试次数。"""
        stale_paths = self.evidence_store.stale()
        if not stale_paths:
            return {"allowed": True, "stale_paths": [], "retry_count": self.retry_count}
        self.retry_count += 1
        return {
            "allowed": False,
            "stale_paths": stale_paths,
            "retry_count": self.retry_count,
            "retryable": self.retry_count <= self.max_retries,
        }
