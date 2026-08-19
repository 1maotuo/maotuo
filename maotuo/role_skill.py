"""Role 与 Skill 的轻量管理。

Role 负责身份和工具权限，Skill 只负责领域知识。两者都使用普通 Python
对象承载，避免引入外部 Agent 框架。
"""

import re
from dataclasses import dataclass


# 只读角色不应该被提示词诱导去尝试写文件或执行命令。
READ_ONLY_GUIDANCE_TOOLS = ("write_file", "patch_file", "run_shell")


@dataclass
class Skill:
    """一个按关键词激活的领域知识片段。"""

    name: str
    keywords: tuple[str, ...] = ()
    prompt_fragment: str = ""
    priority: int = 0


@dataclass
class Role:
    """一个可限制工具集合的 Agent 身份。"""

    name: str = "default"
    base_prompt: str = ""
    allowed_tools: tuple[str, ...] | None = None


class RoleSkillManager:
    """根据当前任务生成 Role/Skill 提示片段和工具交集。"""

    def __init__(self, role=None, skills=None):
        self.role = role or Role()
        self.skills = list(skills or [])

    def allowed_tools(self):
        """返回 Role 权限；空值表示不额外收缩 Maotuo 的全局工具集。"""
        if self.role.allowed_tools is None:
            return None
        return tuple(str(name).strip() for name in self.role.allowed_tools if str(name).strip())

    def active_skills(self, user_message):
        """按简单关键词命中 Skill，并按优先级和名称稳定排序。"""
        text = str(user_message or "").lower()
        matched = [
            skill for skill in self.skills
            if any(str(keyword).lower() in text for keyword in skill.keywords)
        ]
        return sorted(matched, key=lambda item: (-int(item.priority), item.name))[:3]

    def prompt_fragment(self, user_message):
        """生成可放入稳定 Prompt 区域的 Role/Skill 内容。"""
        lines = [f"Role: {self.role.name}"]
        if self.role.base_prompt.strip():
            lines.extend(["Role instructions:", self.role.base_prompt.strip()])
        active = self.active_skills(user_message)
        if active:
            lines.append("Active skills:")
            for skill in active:
                lines.extend([f"[{skill.name}]", skill.prompt_fragment.strip()])
        return "\n".join(lines).strip()

    def skill_prompt_fragment(self, user_message):
        """只返回本轮命中的 Skill，供动态上下文注入。"""
        active = self.active_skills(user_message)
        if not active:
            return ""
        lines = ["Active skills:"]
        for skill in active:
            lines.extend([f"[{skill.name}]", skill.prompt_fragment.strip()])
        return "\n".join(lines).strip()

    def is_read_only(self):
        """判断 Role 是否没有任何写入或命令执行工具。"""
        allowed = self.allowed_tools()
        if allowed is None:
            return False
        return not set(allowed).intersection(READ_ONLY_GUIDANCE_TOOLS)

    def filter_guidance(self, guidance):
        """过滤与只读 Role 冲突的已审批提示策略。"""
        text = str(guidance or "").strip()
        if not text or not self.is_read_only():
            return text
        lowered = text.lower()
        if any(re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", lowered) for name in READ_ONLY_GUIDANCE_TOOLS):
            return ""
        return text


def load_role_skill_config(root, config, role_name="", skill_names=()):
    """把 JSON 配置转换成 Role/Skill 对象，供 CLI 和 Runtime 共同消费。"""
    config = dict(config or {})
    role_data = dict(config.get("roles", {}).get(role_name or config.get("role", "default"), {}))
    role = Role(
        name=role_name or config.get("role", "default"),
        base_prompt=str(role_data.get("base_prompt", "")),
        allowed_tools=tuple(role_data.get("allowed_tools", ())) or None,
    )
    selected = set(skill_names or config.get("skills", ()))
    skills = []
    for name, data in dict(config.get("skill_definitions", {})).items():
        if selected and name not in selected:
            continue
        data = dict(data or {})
        skills.append(
            Skill(
                name=str(name),
                keywords=tuple(str(item) for item in data.get("keywords", ())),
                prompt_fragment=str(data.get("prompt_fragment", "")),
                priority=int(data.get("priority", 0)),
            )
        )
    return role, skills
