# Maotuo 私有化部署说明

Maotuo 本身保持本地轻量运行，不要求引入 Web 框架或 Agent 框架。私有化环境只需要准备 Python 3.10+、内网模型 endpoint 和项目工作区。

## 基础配置

```bash
cp .env.maotuo.example .env
python -m maotuo --cwd /workspace/project
```

`maotuo.config.json` 负责 Role、Skill 和项目级行为配置；`.env` 只负责密钥和 Provider 连接信息，真实密钥不得提交到仓库。

## 离线评测

```bash
python -m maotuo --cwd /workspace/project --evaluate all
```

评测结果写入 `.maotuo/evaluations/`，运行审计结果写入 `.maotuo/runs/<run_id>/audit.jsonl`。

## 网络边界建议

生产环境建议在网络层只允许 Maotuo 访问企业模型网关，不允许模型工具直接访问公网；需要联网的能力应当另行增加审批工具和审计策略。
