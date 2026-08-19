# Maotuo Review Pack

## Project pitch

Maotuo is a lightweight local coding agent harness for repository-grounded engineering tasks. It wraps a model with workspace context, explicit tools, state tracking, memory, run artifacts, and benchmark evidence.

## Architecture map

- `maotuo.cli` wires configuration, provider clients, workspace context, and the runtime.
- `maotuo.runtime.Maotuo` coordinates the agent control surface.
- `maotuo.context_manager` builds bounded model context from prefix, memory, history, and the current request.
- `maotuo.tools` defines the explicit tool allowlist used by the runtime.
- `maotuo.run_store` writes per-run artifacts for review and replay.

## Benchmark evidence

Benchmark runs should preserve reproducibility metadata, task rows, summary counts, and failure categories so reviewers can distinguish runtime regressions from task or provider failures.

## Sample run artifact list

- `.maotuo/runs/<run_id>/task_state.json`
- `.maotuo/runs/<run_id>/trace.jsonl`
- `.maotuo/runs/<run_id>/report.json`
