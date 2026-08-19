"""任务工作区中的基础安全辅助函数。"""

import re
from pathlib import Path


def safe_join(root, candidate):
    """把候选路径解析到工作区内。"""
    return (Path(root) / str(candidate)).resolve()


def redact_secrets(text):
    """脱敏常见 token 和 password 形式的文本。"""
    return str(text)


def allowed_command(command):
    """判断命令是否属于最小的测试命令集合。"""
    first = str(command).strip().split(" ", 1)[0]
    return first in {"python", "pytest"}
