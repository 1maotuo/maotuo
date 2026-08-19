"""Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .prompt_prefix import coding_task_guide


DEFAULT_TOTAL_BUDGET = 12000
DEFAULT_SECTION_BUDGETS = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
DEFAULT_SECTION_FLOORS = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
# 当 prompt 超预算时，会优先压缩这些 section。
DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request")
CURRENT_REQUEST_SECTION = "current_request"
RELEVANT_MEMORY_LIMIT = 3


def _tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self):
        return len(self.raw)

    @property
    def rendered_chars(self):
        return len(self.rendered)


class ContextManager:
    def __init__(
        self,
        agent,
        total_budget=DEFAULT_TOTAL_BUDGET,
        section_budgets=None,
        section_floors=None,
        reduction_order=None,
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = dict(DEFAULT_SECTION_BUDGETS)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)

    def build(self, user_message):
        """按预算组装一轮完整 prompt。

        为什么存在：
        仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、
        哪些旧信息还值得继续参考。这个函数负责把“稳定基线 + 工作记忆 +
        相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了
          预算收缩等信息，后续会进入 trace/report，便于解释这轮 prompt
          是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Maotuo.ask()` 的每轮模型调用之前，是“真正发请求给模型”
        的最后一道组装工序。`WorkspaceContext` 提供稳定前缀，`LayeredMemory`
        提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        coding_prompt_enabled = True
        high_value_context_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
            coding_prompt_enabled = self.agent.feature_enabled("coding_prompt")
            high_value_context_enabled = self.agent.feature_enabled("high_value_context")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}",
        }
        base_prefix = section_texts["prefix"]
        # Role/Skill 与高价值事实放在 prefix 开头；它们在上下文裁剪时优先保留，
        # 避免旧 transcript 挤掉本轮真正需要的信息。Coding 指导保持高优先级，
        # 但不越过 Role/Skill 的硬保护，避免极小预算场景吞掉任务事实。
        priority_blocks = []
        role_skill_prompt = getattr(getattr(self.agent, "role_skill", None), "prompt_fragment", lambda _: "")(user_message)
        if role_skill_prompt:
            priority_blocks.append(
                "Protected Role/Skill policy:\n"
                + role_skill_prompt
            )
        high_value_text, high_value_details = self._high_value_context() if high_value_context_enabled else ("", {})
        if high_value_text:
            priority_blocks.append(high_value_text)
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            # Checkpoint 是断点恢复的当前任务事实，和 Role/Skill 一样不能被旧历史截掉。
            priority_blocks.append(checkpoint_text)
        if priority_blocks:
            priority_text = "\n\n".join(priority_blocks)
            section_texts["prefix"] = priority_text + "\n\n" + base_prefix
            # 保护块自身超过常规预算时允许保留；普通历史会先被压缩。
            protected_length = len(priority_text) + len("\n\n") + 3
        else:
            protected_length = 0
        if coding_prompt_enabled:
            if priority_blocks:
                section_texts["prefix"] = priority_text + "\n\n" + coding_task_guide() + "\n\n" + base_prefix
            else:
                section_texts["prefix"] = coding_task_guide() + "\n\n" + base_prefix
        strategy = self.agent.active_prompt_strategy() if hasattr(self.agent, "active_prompt_strategy") else None
        if strategy and str(strategy.get("proposal", "")).strip():
            # 已审批策略只能作为普通提示指导，不能覆盖 Role、安全规则或工具权限。
            section_texts["prefix"] += "\n\nApproved prompt strategy:\n" + str(strategy["proposal"]).strip()
        stale_paths = list(getattr(self.agent, "pending_stale_paths", []) or [])
        if stale_paths and not high_value_context_enabled:
            section_texts["prefix"] += (
                "\n\nFreshness warning:\n"
                "The following files may be stale. Re-read them before answering:\n"
                + "\n".join(f"- {path}" for path in stale_paths)
            )
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit=RELEVANT_MEMORY_LIMIT)

        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes=selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
                high_value_details=high_value_details,
                coding_prompt_enabled=coding_prompt_enabled,
                high_value_context_enabled=high_value_context_enabled,
                protected_prefix_chars=protected_length,
            )
            return prompt, metadata

        budgets = dict(self.section_budgets)
        if protected_length:
            # 使用本轮局部预算，不修改 ContextManager 的默认配置，避免跨请求污染。
            budgets["prefix"] = max(int(budgets.get("prefix", 0)), protected_length)
            floors = dict(self.section_floors)
            floors["prefix"] = max(int(floors.get("prefix", 0)), protected_length)
        else:
            floors = self.section_floors
        rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 如果 prompt 超预算，就按固定顺序不断压缩。
        # 这里的顺序体现了平台偏好：
        # 先牺牲 relevant_memory，再牺牲 history，然后才动 memory 和 prefix。
        # 最新用户请求永远不裁剪，因为那是本轮最重要的输入。
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in self.reduction_order:
                floor = int(floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow,
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes=selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break

        metadata = self._metadata(
            prompt=prompt,
            rendered=rendered,
            budgets=budgets,
            reduction_log=reduction_log,
            selected_notes=selected_notes,
            user_message=user_message,
            section_texts=section_texts,
            high_value_details=high_value_details,
            coding_prompt_enabled=coding_prompt_enabled,
            high_value_context_enabled=high_value_context_enabled,
            protected_prefix_chars=protected_length,
        )
        return prompt, metadata

    def _high_value_context(self):
        """提取本轮最值得保留的失败、修改和文件新鲜度事实。

        消费者是 ``build`` 的受保护 prefix；调用者仍是 Runtime 每轮的
        ``ContextManager.build``。这里只读取已有 history、Evidence 和工具元数据，
        不执行工具、不改变 AgentLoop 的控制流。
        """
        lines = []
        details = {
            "stale_paths": [],
            "pinned_verification_failure": False,
            "pinned_tool_failure": False,
            "latest_change_paths": [],
            "evidence_paths": [],
        }
        stale_paths = [str(path).strip() for path in getattr(self.agent, "pending_stale_paths", []) or [] if str(path).strip()]
        if stale_paths:
            lines.extend(
                [
                    "Freshness warning:",
                    "The following files may be stale. Re-read them before answering:",
                    *[f"- {path}" for path in stale_paths],
                ]
            )
            details["stale_paths"] = stale_paths

        history = list(getattr(self.agent, "session", {}).get("history", []))
        latest_failure = self._latest_verification_failure(history)
        if latest_failure:
            command, output = latest_failure
            lines.extend(
                [
                    "Latest verification failure:",
                    f"- command: {command}",
                    f"- result: {_tail_clip(output, 700)}",
                ]
            )
            details["pinned_verification_failure"] = True

        latest_tool_failure = self._latest_tool_failure(history)
        if latest_tool_failure:
            name, args, output = latest_tool_failure
            lines.extend(
                [
                    "Latest tool feedback requiring a different action:",
                    f"- tool: {name} {args}",
                    f"- result: {_tail_clip(output, 500)}",
                ]
            )
            details["pinned_tool_failure"] = True

        changed_paths = self._latest_change_paths(history)
        if changed_paths:
            lines.extend(["Latest requested file change:", *[f"- {path}" for path in changed_paths]])
            details["latest_change_paths"] = changed_paths

        evidence = getattr(getattr(self.agent, "evidence_store", None), "to_dict", lambda: {})()
        evidence_items = list(sorted(dict(evidence or {}).items()))[-6:]
        if evidence_items:
            lines.append("Observed file evidence (path -> SHA256 prefix):")
            for path, item in evidence_items:
                digest = str(dict(item or {}).get("sha256", ""))[:12]
                source = str(dict(item or {}).get("source", ""))
                lines.append(f"- {path} -> {digest} ({source})")
            details["evidence_paths"] = [path for path, _ in evidence_items]

        if not lines:
            return "", details
        return "High-value run context:\n" + "\n".join(lines), details

    @staticmethod
    def _latest_verification_failure(history):
        """返回最近一次失败 shell 验证，避免旧报错被完整 transcript 淹没。"""
        for item in reversed(history):
            if item.get("role") != "tool" or item.get("name") != "run_shell":
                continue
            output = str(item.get("content", "")).strip()
            lowered = output.lower()
            failed_exit_code = "exit_code:" in lowered and "exit_code: 0" not in lowered
            if failed_exit_code or lowered.startswith("error:"):
                command = str(dict(item.get("args", {})).get("command", "shell")).strip() or "shell"
                return command, output
        return None

    @staticmethod
    def _latest_change_paths(history):
        """返回最近一次写入工具涉及的路径，提示模型围绕刚修改的文件继续判断。"""
        for item in reversed(history):
            if item.get("role") != "tool" or item.get("name") not in {"write_file", "patch_file"}:
                continue
            path = str(dict(item.get("args", {})).get("path", "")).strip()
            return [path] if path else []
        return []

    @staticmethod
    def _latest_tool_failure(history):
        """返回最近一次非验证工具拒绝，避免模型重复触发同一种无效动作。"""
        for item in reversed(history):
            if item.get("role") != "tool" or item.get("name") == "run_shell":
                continue
            output = str(item.get("content", "")).strip()
            if output.lower().startswith("error:"):
                name = str(item.get("name", "tool"))
                args = json.dumps(dict(item.get("args", {})), ensure_ascii=False, sort_keys=True)
                return name, args, output
        return None

    def _render_sections_without_reduction(self, section_texts, selected_notes=None):
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {note['text']}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw=section_texts["prefix"], budget=len(section_texts["prefix"]), rendered=section_texts["prefix"], details={}),
            "memory": SectionRender(raw=section_texts["memory"], budget=len(section_texts["memory"]), rendered=section_texts["memory"], details={}),
            "relevant_memory": SectionRender(
                raw=relevant_raw,
                budget=len(relevant_raw),
                rendered=relevant_raw,
                details={
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw=history_raw, budget=len(history_raw), rendered=history_raw, details={"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }

    def _compute_section_floors(self):
        floors = {
            section: max(20, int(budget) // 4)
            for section, budget in self.section_budgets.items()
        }
        floors.update(self._section_floor_overrides)
        return floors

    def _render_sections(self, section_texts, budgets, selected_notes=None):
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                raw = section_texts[section]
                rendered[section] = SectionRender(raw=raw, budget=0, rendered=raw, details={})
            elif section == "relevant_memory":
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, int(budget)) if budget is not None else raw
                rendered[section] = SectionRender(raw=raw, budget=int(budget) if budget is not None else 0, rendered=rendered_text, details={})
        return rendered

    def _render_relevant_memory(self, selected_notes, budget):
        header = "Relevant memory:"
        note_texts = [str(note.get("text", "")) for note in selected_notes if str(note.get("text", "")).strip()]
        raw_lines = [header] + [f"- {text}" for text in note_texts]
        raw = "\n".join(raw_lines) if note_texts else "\n".join([header, "- none"])
        if not note_texts:
            rendered = raw
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )

        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉。
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget,
            },
        )

    def _per_note_budget(self, budget, note_count, header):
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)

    def _render_history_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if not history:
            rendered = "Transcript:\n- empty"
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered=rendered,
                details={
                    "rendered_entries": [],
                    "older_entries_count": 0,
                    "collapsed_duplicate_reads": 0,
                    "reused_file_summary_count": 0,
                    "summarized_tool_count": 0,
                },
            )

        # 优先保留最近的历史，因为下一步决策通常最依赖刚刚发生的工具结果。
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw=raw,
            budget=budget,
            rendered=rendered,
            details={
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _compressed_history_entries(self, history, recent_start):
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }

        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 900
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit),
                    }
                )
                continue

            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent": False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue

            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue

            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})

        return entries, details

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), {})
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}"
            content = _tail_clip(item["content"], max(20, line_limit))
            return [prefix, content]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def _assemble_prompt(self, rendered):
        # 顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _metadata(
        self,
        prompt,
        rendered,
        budgets,
        reduction_log,
        selected_notes,
        user_message,
        section_texts,
        high_value_details=None,
        coding_prompt_enabled=False,
        high_value_context_enabled=False,
        protected_prefix_chars=0,
    ):
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars,
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        return {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_sources": [str(note.get("source", "")).strip() for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "high_value_context": {
                "enabled": bool(high_value_context_enabled),
                "coding_prompt_enabled": bool(coding_prompt_enabled),
                "protected_prefix_chars": int(protected_prefix_chars),
                **dict(high_value_details or {}),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            },
        }
