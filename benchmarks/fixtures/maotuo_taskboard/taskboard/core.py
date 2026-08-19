"""任务管理核心逻辑，作为真实 Coding Agent 评测的代码工作区。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    """一个不可变的任务对象。"""

    task_id: int
    title: str
    done: bool = False


def normalize_title(title):
    """清理任务标题的首尾空白。"""
    return str(title).strip()


def add_task(tasks, title):
    """追加任务并返回新列表。"""
    normalized = normalize_title(title)
    if not normalized:
        raise ValueError("title must not be empty")
    next_id = len(tasks) + 1
    return [*tasks, Task(next_id, normalized)]


def complete_task(tasks, task_id):
    """完成指定任务；找不到任务时保持原列表。"""
    return [
        Task(item.task_id, item.title, True) if item.task_id == task_id else item
        for item in tasks
    ]


def filter_tasks(tasks, done=None):
    """按完成状态筛选任务。"""
    if done is None:
        return list(tasks)
    return [item for item in tasks if item.done is done]


def summarize(tasks):
    """返回任务总数和已完成数量。"""
    items = list(tasks)
    return {"total": len(items), "completed": sum(item.done for item in items)}


def serialize_task(task):
    """把任务转换成稳定的普通字典。"""
    return {"id": task.task_id, "title": task.title, "done": task.done}


def parse_task_id(value):
    """把外部输入转换成正整数任务 ID。"""
    return int(value)
