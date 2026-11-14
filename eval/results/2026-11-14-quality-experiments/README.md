# 开发期实验记录（不是最终质量成绩）

这些记录保留失败方案及其诊断依据，避免只展示成功试跑。最终代码和恢复账号后的完整结果见上级目录的 `2026-11-14-quality-environment.json`、`2026-11-14-quality-objective.json`、`2026-11-14-quality-holdout.json`。

| 文件 | 用途及限制 |
| --- | --- |
| `quality-pilot.json` | 单题开发试跑：暴露 pytest 导入路径和命令失败识别问题，最终 GraphRecursionError |
| `quality-pilot-2.json` | 单题开发试跑：内部测试通过，外部非法输入验收失败 |
| `quality-pilot-3.json` | 单题开发试跑：生成测试存在错误舍入预期，最终 GraphRecursionError |
| `quality-objective-diagnostic.json`、`quality-holdout-diagnostic.json` | 发现原始换行 JSON 问题后停止的部分轮次；`complete=false`，取消中的试次未计入摘要；不能以其分数代替完整结果 |
| `quality-run-environment.json` | 上述部分轮次的代码文件哈希与环境 |
| `quality-objective-strict.json`、`quality-holdout-strict.json` | 严格阻塞方案的 12 次和 6 次试跑，均未通过；各前 3 次为 QualityBlocked，其后为模型接口 BadRequestError。随后单独探测确认 Arrearage；原始每条 BadRequestError 未保留错误码，不能逐条证明原因 |
| `quality-final-environment.json` | 严格阻塞方案轮次环境；文件名 final 是开发期命名，不代表最终发布实现 |
| `quality-proxy-interrupted.json` | 最终实现首次原题复测遇到 Docker Desktop 管理代理无响应；保存的 9 次结果含超时，剩余轮次未完整结束。用户授权重启后另开完整 12 次原题评测，新增题已完成的 6 次保留独立统计 |
| `quality-account-interrupted.json` | Docker 恢复后的原题评测在第 4 次遇到 Arrearage，自动停止。用户再次恢复账号后通过显式续跑执行剩余 8 次；前 4 次（包括账号失败）原样保留，不重新挑选结果 |

严格阻塞方案混有账号故障，不能将其 0/12、0/6 当作纯粹代码能力结论。账号恢复后，在独立新文件中从头运行完整评测，未覆盖这些记录。

原始 JSON 包含固定合成任务的生成代码、内部测试和有界轨迹。公开前已按本机已配置的凭据值扫描；模型调用账单和包含环境细节的控制台日志保留在本地。
