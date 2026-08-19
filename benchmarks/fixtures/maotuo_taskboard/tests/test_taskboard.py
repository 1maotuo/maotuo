"""本地任务工作区的基础回归测试。"""

from taskboard.config import load_config
from taskboard.core import Task, add_task, complete_task, summarize
from taskboard.service import build_report


def test_add_and_complete_task():
    tasks = add_task([], "write tests")
    assert tasks == [Task(1, "write tests")]
    assert complete_task(tasks, 1) == [Task(1, "write tests", True)]


def test_config_has_safe_defaults():
    config = load_config()
    assert config["timeout"] == 30
    assert config["max_tasks"] == 100


def test_summary_and_report_are_structured():
    tasks = [Task(1, "one"), Task(2, "two", True)]
    assert summarize(tasks) == {"total": 2, "completed": 1}
    assert build_report(tasks)["summary"] == {"total": 2, "completed": 1}
