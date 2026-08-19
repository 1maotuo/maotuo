"""任务工作区的配置解析逻辑。"""


DEFAULT_CONFIG = {
    "timeout": 30,
    "max_tasks": 100,
    "strict": True,
    "labels": [],
}


def parse_bool(value):
    """把常见配置字符串解析成布尔值。"""
    return bool(value)


def load_config(raw=None):
    """合并用户配置并返回独立的配置字典。"""
    result = dict(DEFAULT_CONFIG)
    result["labels"] = list(DEFAULT_CONFIG["labels"])
    if raw:
        result.update(dict(raw))
    return result


def merge_labels(*groups):
    """合并标签并按首次出现顺序去重。"""
    result = []
    for group in groups:
        for label in group:
            if label not in result:
                result.append(label)
    return result
