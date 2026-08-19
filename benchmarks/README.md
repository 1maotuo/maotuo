# Maotuo Benchmark

## 本地真实任务集

`maotuo_real_tasks.json` 是一个不依赖外部开源仓库的本地真实代码任务集。
它使用一个带有源代码和 pytest 测试的小型 Python 工作区，模型需要真实读取、修改和运行测试。

任务集包含 24 个任务，覆盖代码修复、测试修复、受控修改、安全审查和恢复场景。
每个任务都有独立的工作区副本、允许工具集合、步数预算、验收文件和 verifier 命令。

校验任务格式和本地工作区：

```bash
python -m maotuo.evaluation.real_benchmark --validate-only
Push-Location benchmarks/fixtures/maotuo_taskboard
python -m pytest -q
Pop-Location
```

真实消融实验固定使用 DeepSeek Flash。先运行 8 个任务的小样本，确认趋势后再运行完整任务集：

```bash
python -m maotuo.evaluation.real_benchmark \
  --provider deepseek \
  --model deepseek-v4-flash \
  --ablation \
  --pilot \
  --repetitions 1 \
  --artifact-root artifacts/maotuo-ablation-pilot
```

四组使用同一任务、同一 Flash 配置、同一温度和同一预算：Baseline、Prompt Only、Prompt + Context、Full。
基础路径校验、审批与脱敏始终开启；Full 在此基础上增加 Evidence 和 ReleaseGate。真实运行产物默认被 `.gitignore` 忽略。

完整评测使用 24 个任务、每组 2 次重复：

```bash
python -m maotuo.evaluation.real_benchmark \
  --provider deepseek \
  --model deepseek-v4-flash \
  --ablation \
  --repetitions 2 \
  --artifact-root artifacts/maotuo-ablation-full
```

生成四组消融报告：

```bash
python -m maotuo.evaluation.benchmark_report \
  --ablation artifacts/maotuo-ablation-full/ablation-summary.json \
  --output artifacts/maotuo-ablation-full/ablation-report.md
```

## 受控自进化评测

`maotuo_evolution_cases.json` 固定 4 个训练失败场景和 4 个封闭 ReplayCase。
候选提示策略只能在训练集上生成和调整，最终必须同时满足训练集改善、封闭集不退化、
GoldenTask 全部通过和人工审批，才允许进入 active 状态。

简历中的自进化效果必须来自候选前后真实运行的通过率、工具调用次数和安全回归结果，
不能只用“生成了多少候选”作为效果指标。

真实模型运行自进化 A/B：

```bash
python -m maotuo.evaluation.evolution_benchmark \
  --provider deepseek \
  --repetitions 1 \
  --output-root artifacts/maotuo-evolution-v1
```

该命令只在评测副本中注入候选指导，不会自动把候选写成生产 active 策略。

## 数据口径

真实任务报告只统计真实模型运行结果；`FakeModelClient` 只用于 Runtime Contract 和离线回归测试。
报告中的任务成功必须同时满足：Verifier 通过、在步数预算内完成、工作区验收文件存在、Run 没有以模型或重试错误结束。

从数据校准版本开始，评测器还会记录任务开始前的 Verifier 结果、目标文件修改前后 SHA256，
并单独计算 `quality_pass_rate`。如果任务在模型执行前就已经通过 Verifier，或模型没有改变目标文件，
该任务不会计入有效任务通过率；原始 `pass_rate` 和完整失败工件仍然保留，便于审计和复盘。

对外 Markdown 报告将模型标记为 `DeepSeek`；`ablation-summary.json` 仍保留精确模型名称、Provider 参数和特性开关，保证评测可复现。
