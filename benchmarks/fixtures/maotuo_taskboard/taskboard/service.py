"""任务服务层逻辑，用于验证模型跨文件理解和修改能力。"""

from .core import serialize_task


def export_tasks(tasks):
    """导出任务列表，并按任务 ID 排序。"""
    return [serialize_task(item) for item in tasks]


def retryable_error(error):
    """判断错误是否适合自动重试。"""
    return isinstance(error, TimeoutError)


def build_report(tasks):
    """生成稳定的任务报告。"""
    return {
        "summary": {
            "total": len(tasks),
            "completed": sum(item.done for item in tasks),
        },
        "tasks": export_tasks(tasks),
    }
