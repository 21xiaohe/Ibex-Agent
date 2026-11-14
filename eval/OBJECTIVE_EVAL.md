# 固定开发任务评测

`ibex-dev-v1` 是一个小规模、可重复执行的研发任务集，不是通用能力榜单。它和原来的 LLM 裁判评测分开记录，不比较或沿用历史 `baseline.json` 分数。

## 任务与评分

六个任务固定在 `objective_cases.py`：金额汇总、折扣舍入、分页缺陷修复、事件幂等、FastAPI 输入校验、有限重试。每题给出明确输入、输出、错误处理要求。每次独立创建沙箱；单 Agent 执行，每题默认重复两次。

验收断言在生成结束后才注入执行，不放入模型提示词或沙箱初始文件。评分器使用独立 Python 进程，忽略项目 pytest 配置；要求目标文件存在、验收进程退出码为零、所有验收断言通过，才算一次任务成功。参考实现通过/错误实现失败的自检纳入普通 CI。

- **任务成功率** = 所有通过的试次 / 所有计划且实际执行的试次。超时、模型错误、沙箱异常都计失败，不挑最好一次。
- **每题通过次数**用于展示重复执行的波动。2 次重复不能支持稳定性或显著性结论。
- 到达验收阶段的试次保存代码产物、断言结果、退出码、耗时、工具调用数及模型提供的 token 计数。提前超时或达到图执行上限的试次记录错误类型与耗时，可能没有代码产物及完整 token 统计。token 统计不是账单金额。
- AST 规范检查仅作为辅助信息，不参与任务通过判定；它不能完整判断金额运算是否符合 Decimal 规范。
- 这些是对非恶意生成代码的功能验收，不是抵御评测器攻击的安全基准，也不覆盖团队模式、长任务或大仓库开发。

## 运行

先配置本地模型和 Docker 沙箱；运行会调用付费模型 API。不要在公开 issue、日志或版本库里填写密钥。

```bash
docker build -t ibex-sandbox:local -f sandbox/Dockerfile .
python -m eval.run_objective --repeats 2 --timeout 300 --output logs/objective-run-001.json
```

已有 Compose 部署也可在 Web 容器中运行同一命令，以使用部署配置的沙箱服务。每次选择新输出文件，评分器拒绝覆盖已有证据。每次试验完成后写入结果；只有含 `complete: true` 的记录才代表整轮结束。默认全部通过才返回退出码 0；失败结果也会完整保存。

```bash
python -m eval.run_objective --repeats 2 --output logs/objective-run-002.json --baseline logs/objective-run-001.json
```

基线比较要求任务集哈希、评分器哈希和记录的运行配置相同。还应人工确认 Agent 源码、依赖、备用模型和运行环境差异；报告中的成功率差值不代表统计显著提升。不要为得到绿灯而降低门槛或删掉失败试次。

## CI

- `Regression tests`：push / PR 自动运行不调用真实模型的回归测试，包括评测器和验收断言自检，并上传 JUnit 结果。
- `Objective model evaluation (manual)`：手动触发真实模型评测，默认 6 题 × 2 次，全部通过才成功；无论通过与否都上传原始 JSON。需要自行配置仓库 Secret `IBEX_API_KEY`，以及可选变量 `IBEX_MODEL_NAME`、`IBEX_BASE_URL`。普通 push 不调用付费模型。

本机真实运行的结果与失败分析见项目根目录 `FOLLOWUP_REPORT.md`。发布的原始评测文件不含模型密钥。

2026-11-14 的质量修复与面试说明见 `INTERVIEW_REPAIR_NOTES.md`。本轮评分器增加异常前的代码/工具轨迹采集，原 6 题定义与 `acceptance_command` 保持不变。因评分器文件哈希改变，旧基线不直接传入 `--baseline`；报告中单独核对任务和验收命令一致性并列比较，不能把不同哈希当作完全相同的运行器。

补充的新任务使用 `python -m eval.run_holdout --repeats 2 --output logs/new-tasks.json`。该小集合覆盖区间合并、库存原子更新和时区转换，与原集合分开统计。它是新任务验证，不是双盲或统计独立的能力评测。
