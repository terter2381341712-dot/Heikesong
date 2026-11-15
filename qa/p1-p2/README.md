> 第三阶段更新（`zyc`）：P2 已按已验证 CPU/GPU 能力、剩余槽位与真实执行耗时调度，支持历史持久化和有界并发对账。公开契约仍为 0.3.0。详见 [使用说明](../../P2/docs/资源感知调度.md) 与 [验收报告](资源感知调度实施报告.md)。

> 第二阶段更新：已实现 CPU 多槽、线程预算和 P2 剩余容量派发；GPU 保持单卡单槽。公开契约升级为 0.3.0，P1/P2/节点须同步升级。当前启动方式与行为以 [多槽使用说明](../../P1/docs/CPU多槽与线程预算.md) 为准，以下历史阶段记录保留供追溯。

> 2026-11-14 后续实现更新：计算终止、共享关闭与 P2 迁移已实现，新增交互式 CLI；当前行为、接口和测试范围以 [实施报告](共享开关实施报告.md) 为准。以下此前未实现/原样导入描述保留为阶段背景。

# P1–P2 统一联调入口

本次基础联调依据：[统一修改计划 1.0](../../归档/P1-P2统一修改与联调计划.md)。本目录对应计划 P2-F，由 P2 汇总共同样例、规则澄清和验收入口；P1 实现与自检仍由队友负责。用户后续确认的共享开关及立即停止迁移需求见 [补充计划](../../归档/P1-P2设备共享开关与任务迁移补充计划.md)，它尚未进入当前实现。

当前公开接口使用 [OpenAPI 0.3.0](../../D题参赛执行包/contracts/openapi.yaml)，SHA-256：`e73ea0667ef0474e42c0efdc6d45f5e74cfd03fa432be811c8359776c16ad9de`。CPU 多槽扩展及升级要求见 [使用说明](../../P1/docs/CPU多槽与线程预算.md)。

## 当前真实联调结果与启动入口

2026-11-14 已连接指定 GitHub 仓库取得 `wzq@7368ad0`，将其 P1 所属文件原样放入 [P1/](../../P1/README.md)。真实单机 OCR 闭环、暂停后恢复执行、终态结果离线下载及重启后重新准备已通过。完整范围见 [实际联调报告](实际联调报告.md)，去凭证记录见 [real-integration-result.json](real-integration-result.json)。这不是 L01–L19 全量通过或物理两机验收。

先按 P1/P2 README 分别安装环境，再从项目根目录执行：

```sh
P2/.venv/bin/python qa/p1-p2/run_local.py check
P2/.venv/bin/python qa/p1-p2/run_local.py serve
```

- `check` 使用随机空闲端口和新的 `var/check-*/`，退出时停止本次启动的全部进程；失败返回 1，中断返回 130，不把未执行项算通过。
- `serve` 明确启动本机共享，默认端口 8001/8002、数据目录 `var/live/`，Ctrl+C 停止。它不是安装后自动启动的客户端，也没有实现关闭共享后的任务迁移。
- 可使用 `--data-dir`、`--resource-port`、`--task-port`；同一数据目录不能同时启动两组服务，端口占用时失败退出，不停止已有服务。重启 `serve` 沿用原目录。
- 地址均为 `127.0.0.1`，此工具不宣称跨机可达；跨机启动沿用计划第 3.2 节和 P1 启动文档。
- `connection.json` 保存三个随机角色令牌（创建权限 0600）；仅向 P2 注入 DEMO/TASK，向节点注入 JOIN。`services.json` 记录本次启动的 PID 和地址，日志在同一目录。数据库、令牌和日志均被 Git 忽略。
- 本机子进程统一使用 `NO_PROXY/no_proxy=127.0.0.1,localhost`，避免继承含 IPv6 的代理配置导致 httpx 初始化失败；不会改动系统或终端环境。

针对连接工具的测试与 P2 回归，从 `P2/` 执行：

```sh
.venv/bin/python -m pytest -q tests/tasks ../qa/p1-p2/test_run_local.py
.venv/bin/ruff check --config pyproject.toml task_service splitters aggregators tests/tasks ../qa/p1-p2/run_local.py ../qa/p1-p2/test_run_local.py
.venv/bin/ruff format --check --config pyproject.toml task_service splitters aggregators tests/tasks ../qa/p1-p2/run_local.py ../qa/p1-p2/test_run_local.py
```

本次为 P2 **96 项**与连接工具 **9 项**全部通过；P1 独立环境中的 **51 项**全部通过。

## 共享输入与语义

- [capabilities.json](capabilities.json)：两种 NOT_PREPARED、PREPARING、READY、ERROR、UNAVAILABLE 的完整 Capability 对象。它们是资源快照内的条目，不是完整 HTTP 响应。
- [P2 测试副本](../../P2/tests/tasks/fixtures/p1-p2-capabilities.json) 与共享文件逐字节一致，SHA-256：`146c12e35cb865db8a45425fe60f32c3c066fd1474fe1295accd58daa719fa1d`。全部为合成样例，不能作为真实权重、自测、设备或识别结果证据。
- 请求/响应其余样例沿用 [原始 examples.json](../../D题参赛执行包/contracts/examples.json)，未修改原文件。
- 选择规则以计划第 3.4 节为准；空槽与 READY 的边界、自动准备顺序及 X02 归属已同步到 [接口手册](../../D题参赛执行包/05-接口契约与联调手册.md)。X01 由 P1 主责，X02 由 P2 主责，双方配合对账。
- 配置、公共接口清单及交接材料见 [P2 → P1 交接清单](../../P2/docs/P1-联调交接清单.md)。两服务独立环境，环境变量需要显式注入；真实令牌不写入证据文件。

## 上一轮 P2 协议回归记录

以下从项目根目录执行，不需要真实 P1、节点或模型：

```sh
cd P2
uv run --frozen pytest -q tests/tasks/test_plan_preparation.py
uv run --frozen pytest -q --junitxml=docs/p2-plan-pytest.xml
uv run --frozen ruff check task_service splitters aggregators tests/tasks
uv run --frozen ruff format --check task_service splitters aggregators tests/tasks
```

本次完整回归 **96 passed，0 failed，0 skipped**，含新增 39 项；ruff 检查通过。完整命令、范围及结果见 [P2 实施报告](../../P2/docs/P2-统一计划实施报告.md)，原始结果见 [JUnit](../../P2/docs/p2-plan-pytest.xml)。[两种冷节点证据](../../P2/docs/p2-plan-mock-evidence.json) 为测试结束后的 P2 持久化快照，记录 Job/Task/preparation/execution/worker/session/input/result 的关联。

| 联调用例 | 本次 P2 协议覆盖 | 尚需真实或 P1 侧证据 |
|---|---|---|
| L01 | P2 health、目录、角色及 Error 回归通过 | 真实地址、P1 鉴权与版本 |
| L02 | 未验证真实上传/下载 | P1 原始字节、SHA、元信息与跨调用端下载 |
| L03 | 两种未准备形态自动准备→执行→结果/ZIP，通过 | 真实新节点 NOT_PREPARED/null 起步的完整闭环 |
| L04 | 准备/无资源/清理/占槽/离线/模式/失败等待与恢复，通过 | 对接真实资源事实 |
| L05 | 错摘要、空摘要、未验证、失败/缺失自测、缺少实际后端证据不派发，通过 | P1 对实际权重与锁定环境的核验 |
| L06–L07 | P2 跨 Job 避免重复准备；未验证 P1 原子接纳 | P1 一槽并发、同 ID 并发及拒绝记录不可再接受 |
| L08 | 准备/执行未知结果原 ID 对账、GET 404 重放、拒绝退避与计数，通过 | P1 缺失引用拒绝落库、真实响应丢失 |
| L09 | 不属于 P2 实现 | P1 租约、硬期限、旧会话与最终提交仲裁 |
| L10 | 坏 SHA/schema/类型/输入/尺寸/bbox 拒收，通过 | P1 发布持久化、成功结果重放与提交权 |
| L11 | P2 取消阶段、未知接纳、成功竞争与结果保留，通过 | P1 实际停止与失联撤权 |
| L12 | P2 按 PAUSED/DRAINING 等待并允许状态恢复，通过 | 真实节点恢复 ACTIVE 后实际领取并完成 |
| L13 | P2 避开 OFFLINE/cleanup，依赖故障保留原执行，通过 | 真实失联时限与旧工作清理屏障 |
| L14 | 模拟明确 RESOURCE_SERVICE_RESTARTED 后才重试，通过 | P1 重启的终态、文件、会话及幂等持久性 |
| L15 | P2 准备/派发/结果阶段重启、拒绝与最多三次已接受尝试，通过 | 组合真实服务恢复记录 |
| L16 | 原结果重取、无重复推理、P1 离线后 results/ZIP，通过 | P1 真实结果文件持久性 |
| L17 | 模拟两节点调度回归通过，不代替物理多机 | 两台物理电脑各产生同一 Job 的有效最终结果 |
| L18 | 不属于 P2 实现 | P1 离线基线、manifest 关联及退出状态 |
| L19 | 本次回归失败时 pytest 非零退出，最终完整通过 | P1 自检失败/中断/未执行项的退出行为 |

**上表保留上一轮仅 P2 协议回归的范围，当时真实组合均为 NOT_RUN。** 本轮已有上述真实单机记录；具体通过子项及剩余 NOT_RUN 以 [实际联调报告](实际联调报告.md) 为准，不能据单元测试签收完整联合验收。

## 真实单节点与两机复验步骤

由 P1 提供其当前 commit、独立环境启动方式、可达地址、真实 profile 锁文件与 digest，并提供符合计划的新节点状态。P2 启动命令为 `uv run --frozen python -m task_service`，环境按 [P2 README](../../P2/README.md) 注入，停止使用正常进程终止信号，重启沿用 TASK_DATA_DIR。

1. 从 P2 所在电脑检查两服务 healthz 和 P1 task-types/resources：版本为 0.3.0、目录 CONFIGURED、节点 ONLINE/ACTIVE、空槽、NOT_PREPARED/null。按 L01 检查 401/403。
2. 用 DEMO_TOKEN 向 P1 `POST /api/resource/v1/artifacts`，multipart 字段为新的 UUID `upload_id` 与真实 JPG/PNG `file`；保存 INPUT Artifact，再从另一调用端下载并比对原始 SHA。
3. 用 DEMO_TOKEN 向 P2 `POST /api/task/v1/jobs`。请求字段只有 `client_request_id/task_type/profile_id/profile_digest/input_artifact_ids/parameters`，目标配置取刚读取的真实目录；语言取目录实际支持值。
4. 客户端只查询 Job、资源与已存在的 preparation/execution；**不手工 POST preparation/execution**。观察 P2 自动准备时 attempts_used=0、请求摘要等于 Job，真实 READY 后才有接受执行。
5. 单图 Job 成功后 GET `jobs/{id}/results`、`jobs/{id}/download`；按 L03 核对单输入接受次数为 1，输入/profile/worker/session/结果关联及 JSON、SHA、ZIP 顺序。
6. 让第二台物理电脑上线，提交同一 Job 的多张不同图片；确认每台至少贡献一份有效最终结果并按 input_index 汇总。不能将同机两个进程算作 L17。
7. 依计划第 6 节逐项执行其余故障、取消、重启和基线用例。P2 发起/恢复 Job 与 X02；P1 执行节点、资源服务和 X01 操作。失败归属到相应模块，不放宽调用方校验。

每条联合记录按计划保存：用例 ID、双方 commit、计划/契约版本、模式、物理设备、UTC 起止时间、执行命令、去凭证的请求响应、ID/SHA 关联、预期/实际、PASS/FAIL/NOT_RUN 和日志位置。未执行项继续记录 NOT_RUN；本次保持引入的 P1 原始实现不变，使用其真实执行器验证已列出的联合路径。

第四阶段可运行 `P2/.venv/bin/python -B qa/p1-p2/check_input_prefetch.py`：真实 CPU 单槽使用 GPU 相同输入路径，检查缓存命中、跨进程重叠、模型复用、取消及关闭共享迁移。见 [输入流水线实施报告](GPU输入流水线实施报告.md)。Windows GPU 吞吐仍需实机对照。
