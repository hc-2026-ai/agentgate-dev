# 用例等权总分实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 任务总分等于各条有有效得分用例的综合分的平均值。

**Architecture:** 在后端报告汇总入口复用现有按 case 聚合的函数，只修改 overall 的分数来源。指标、维度、评估器类型分项继续保留，前端沿用现有单样本综合分和状态计算。

**Tech Stack:** Python、Pydantic、pytest、FastAPI、Vue、Playwright。

**Spec:** 下述计分约定是本次修改范围。

## 计分约定

- 每条用例先对任务所选主评估器的非空得分求平均，再对有得分的用例求平均。
- 0 分参与；不适用、执行异常等没有数值的结果不参与；完全没有得分时为 null。
- LLM 组合按父评估器的最终分计入，保留组合内部权重，子项不重复计入。
- 不先对分数四舍五入；通过、失败、异常、待复核状态继续独立判断。
- 规则、LLM、混合三类任务使用同一总分公式，使用全任务结果。

## Global Constraints

- 从已更新的 refactor-1 创建 feature/case-equal-mean-score，修改保持在当前工作目录。
- 不修改评估器执行、登录模式路由、任务快照和执行记录，不新增算法兼容分支。
- 门槛判断使用报告的新 overall；前端快照说明同步准确描述计分方式。
- 摘要缓存升级至版本 2；复用现有 backfill 重建旧摘要，原始 Run/Results 不变。

## Review Focus

- 不同用例适用评估器数量不同，仍须保持用例等权。
- 0 分、无分数和没有任何有效得分的任务。
- 同一个 metric 下的不同主评估器，以及 LLM 组合依赖项。
- 样本失败或待复核有分数时不得被改成通过。
- API、门槛判定和页面显示使用同一个总分。

## Task 1: 修改总分并验证报告

**Files:**
- Modify: `src/agentgate/result/metrics.py`
- Modify: `frontend/src/views/evaluation/components/TaskResults.vue`
- Modify: `src/agentgate/storage/evaluation_query_models.py`
- Test: `tests/test_metrics.py`, `tests/test_metrics_api.py`
- Test: `tests/test_evaluation_summary.py`

**Interfaces:** 保留 calculate_metrics 的输入、输出和分项统计结构；前端读取 metrics 中 level=overall 的 score。

- [x] 增加回归测试：截图八条用例总分为 0.6610416666666666；规则、LLM 和混合评分；适用项不等；同 metric 多主项；0/null/依赖排除。
- [x] 运行测试，确认旧维度平均算法在新期望上失败（7 项预期失败）。
- [x] 复用 _score(primary) 计算 overall，保留原有分项和计数。
- [x] 本地真实 API 验证截图八样本，确认报告、release_gate、任务列表和总览得分一致。
- [x] 更新快照中的汇总说明；运行后端测试、前端检查、真实浏览器验证。
- [x] 独立审查最终差异，保留可核对的本地验证证据。

## 验证结果与部署

- 评分与报告定向回归 32 项通过；摘要相关回归 43 项通过、10 项环境跳过。
- 前端 lint/typecheck 通过，126 项单测通过；Edge 验证桌面和移动端，总分显示 66.1，筛选不改变任务总分，控制台无错误。
- 完整后端回归：2889 passed、102 skipped、19 failed、2 errors。原始 c1f2139 快照复现全部失败类型：Windows 符号链接/权限/POSIX 能力、超长参数环境变量，以及两项轨迹排序不稳定。
- 新算法统一用于当前和历史报告，未修改 RunManifest 或持久化执行证据。摘要版本 1 由列表识别为待重建，避免旧总分与新详情混用。
- 部署 API 和 worker 新代码后，使用 `scripts/upgrade/20261021/backfill_evaluation_summaries.py` 重建历史摘要。按现有工具流程先 `--check`，再 `--apply --batch-size 50`，最后 `--check`；每次指定 `--report`，SQLite 使用 `--sqlite`，MySQL 使用 `--env-file`。此操作只更新可重建的查询投影，不重跑智能体或评估器。
