> 集成版默认 `TASK_SCHEDULER_POLICY=round_robin`，资源感知需显式设为 `resource_aware`；两种策略均使用容量、就绪与安全检查。P4 启动请使用 [计算配置和逐阶段验收](../P4/集成验收.md)。

> 第四阶段更新（`zyc`）：已实现单槽输入预取与 CPU 解码/推理重叠，保留取消和迁移。当前契约 **0.4.0**；Windows GPU 吞吐待实机验收。见 [启动与接口说明](../P1/docs/GPU输入预取与解码流水线.md)。下列阶段更新保留为历史记录。

> 第三阶段更新（`zyc`）：P2 已按已验证 CPU/GPU 能力、剩余槽位与真实执行耗时调度，支持历史持久化和有界并发对账。公开契约仍为 0.3.0。详见 [使用说明](docs/资源感知调度.md) 与 [验收报告](../qa/p1-p2/资源感知调度实施报告.md)。

> 第二阶段更新：已实现 CPU 多槽、线程预算和 P2 剩余容量派发；GPU 保持单卡单槽。公开契约升级为 0.3.0，P1/P2/节点须同步升级。当前启动方式与行为以 [多槽使用说明](../P1/docs/CPU多槽与线程预算.md) 为准，以下历史阶段记录保留供追溯。

# Heikesong · P2 OCR 任务服务

本目录集中存放 P2 的代码、依赖配置、测试、开发文档和本地运行数据。**下文命令均在 `P2` 目录执行**；若当前位于项目根目录，请先 `cd P2`。团队公共资料仍在 [原执行包](../D题参赛执行包/README.md)。

面向团队接口契约 **0.4.0** 的 P2 实现：图片批次校验、一图一 Task、资源准备与调度、状态查询、取消、失败重试、重启对账，以及有序 JSON/TXT/ZIP 汇总。

本服务只开放 `ocr.image.v1`。RapidOCR / ONNXRuntime、设备探测和真实图片推理由 **P1** 提供；本仓库中的模拟 P1 仅用于协议自测，不包含真实 OCR、物理节点或性能证据。

目录迁移保留了原有演示数据库、结果和日志；新开发使用本目录下的 `.venv`。`.venv-runtime` 仅供迁移前已启动的后台进程继续使用，相关兼容链接及验证见 [迁移说明](docs/目录迁移说明.md)。

## 环境与启动

Python **3.9.6（完整版本号）**。使用重新解析的 `uv.lock` 固定 P2 HTTP 服务及测试依赖，不安装 OCR 引擎。维护环境时使用 `uv sync --frozen --python 3.9.6`；日常由 P4 启动器自动准备专用环境。

```sh
uv sync --frozen
```

真实联调前填写 `.env.example` 中的配置，并通过 shell 或进程管理器注入环境。程序**不会自动读取 .env**。最少需要 `RESOURCE_BASE_URL`、`TASK_TOKEN`、`DEMO_TOKEN`、`TASK_DATA_DIR`；启动入口：

```sh
uv run --frozen python -m task_service
```

- 默认监听 `0.0.0.0:8002`，可用 `TASK_BIND_HOST`、`TASK_PORT` 修改。
- `RESOURCE_BASE_URL` 只能含协议、主机、端口，不带路径或尾斜杠。跨机器使用对方可达的局域网地址。
- `TASK_DATA_DIR` 必须是独立绝对路径，与 P1 分离；重启使用原目录即可恢复任务及结果。
- **单进程、单 worker**：数据目录有独占锁，避免重复启动调度器。不要使用多个 Uvicorn worker。
- 没有默认令牌；DEMO_TOKEN 与 TASK_TOKEN 必须非空且不同。前端只使用 DEMO_TOKEN。
- P2 没有自动 MOCK 回退。P1 不可达时，新建请求明确报错，已有任务保留原 ID 等待对账。
- `/api/task/v1/healthz` 仅表示 P2 活着。接口规范在 `/openapi.json`，交互文档在 `/docs`；前端按团队约定使用同源代理。

## 独立自测

```sh
uv run --frozen pytest -q
uv run --frozen ruff check task_service splitters aggregators tests/tasks
uv run --frozen ruff format --check task_service splitters aggregators tests/tasks
```

测试既包含 HTTP/ASGI 模拟故障场景，也会启动两个真实本地 HTTP 服务进程，验证建任务、调度、取消、P2 重启和 P1 停止后下载；端口与数据目录隔离，结束时自动停止测试子进程。没有模型下载或外部服务依赖。

只运行进程级自测：

```sh
uv run --frozen python -m tests.tasks.smoke
```

统一计划 1.0 的本次结果见 [P2 修改与回归报告](docs/P2-统一计划实施报告.md)，此前测试保留在 [独立自测报告](docs/P2-独立自测报告.md)。团队原验收矩阵中的真实联调、OCR 质量及多机性能状态没有因此改为 PASS。

只检查本轮准备、选择及接口边界：`uv run --frozen pytest -q tests/tasks/test_plan_preparation.py`。完整回归仍需运行上面的全部 P2 测试，以检查对账、重试预算、取消和结果持久化。

## 手工连接模拟 P1

终端 A 显式开启模拟服务，默认仅监听本机 18001：

```sh
export P2_ENABLE_MOCK_P1=1
export TASK_TOKEN=mock-task-token
export DEMO_TOKEN=mock-demo-token
uv run --frozen python -m tests.tasks.mock_p1
```

终端 B 启动 P2，使用专用测试目录：

```sh
export RESOURCE_BASE_URL=http://127.0.0.1:18001
export TASK_TOKEN=mock-task-token
export DEMO_TOKEN=mock-demo-token
export TASK_BIND_HOST=127.0.0.1
export TASK_PORT=18002
export TASK_DATA_DIR="$PWD/var/p2-mock"
uv run --frozen python -m task_service
```

模拟 P1 预置 `mock_image_1`、`mock_image_2`、`mock_image_3` 三张不同内容的 20×20 PNG，结果明确包含 `MOCK OCR`。它有两个合成节点，状态通过查询推进，**不模拟真实性能**。当前模拟服务不实现上传；开发 P2 时可直接使用预置 artifact ID。

在终端 C 创建测试批次：

```sh
curl -sS http://127.0.0.1:18002/api/task/v1/jobs \
  -H 'Authorization: Bearer mock-demo-token' \
  -H 'Content-Type: application/json' \
  --data-binary @tests/tasks/fixtures/create-ocr-job.json
```

样例中的 profile digest 是明确的合成值，只能用于此模拟服务。同一个 `client_request_id` 重放会返回原 Job；要创建新批次需更换 UUID。根据响应的 `job_id` 查询、取消和下载；真实模式则从 P1/P2 类型目录获取真实 profile，并先通过 P1 上传输入。

## 对外接口

除健康检查外，均要求 `Authorization: Bearer <DEMO_TOKEN>`。

| 方法与路径 | 行为 |
|---|---|
| GET /api/task/v1/healthz | P2 自身健康及契约版本 |
| GET /api/task/v1/task-types | P2 已实现类型与 P1 类型目录的交集，本轮仅 OCR |
| POST /api/task/v1/jobs | 校验并持久创建 Job；首次 201，幂等重放 200，冲突 409 |
| GET /api/task/v1/jobs/{id} | Job、逐图 Task、唯一计数、等待原因及已知执行摘要 |
| POST /api/task/v1/jobs/{id}/cancel | 停止新分派；已终态 200；需要确认的活动取消返回 202 |
| GET /api/task/v1/jobs/{id}/results | 部分/最终结果；每个输入都有一项，未完成结果为 null |
| GET /api/task/v1/jobs/{id}/download | 终态 ZIP；未结束 409；失败及取消也可下载 |

请求与响应按 [原始 OpenAPI](contracts/openapi.yaml) 校验；契约来源及哈希见 [来源记录](contracts/README.md)。数组顺序参与幂等比较，对象键顺序不影响重放。

## 调度与持久化规则

- 一图一 Task，输入顺序从 0 编号，单 Job 1–200 个不同原字节 SHA 的 JPG/PNG。
- 按 Job 创建顺序取待派任务，先匹配类型/profile_id，再区分准备与执行。节点必须 ONLINE、ACTIVE、无清理、无执行/准备占用且有空槽；空槽不要求模型 READY。空闲节点稳定轮询，不按 RAM 比例分整批。
- `NOT_PREPARED` 且 `verified=false` 时，能力摘要为 null 或等于 Job 摘要均可准备；准备请求使用 Job 创建时冻结的非空摘要。非空错摘要不准备、不改 Job。PREPARING 只查询原准备或等待资源更新。
- 只有 READY、摘要与 Job 相同、verified=true、自测通过，且报告 backend/provider/model_sha256 时才执行。缺失或矛盾的信息进入明确等待状态；P2 只检查公开核验证据，由 P1 负责权重和实际环境核验。model_cached 单独不能证明就绪。
- 等待原因区分离线、暂停/排空、清理、占槽、准备中、能力缺失、准备失败及摘要不匹配；ERROR/UNAVAILABLE 保留 P1 原因，也可选择其他合法节点。
- 先在 SQLite 保存完整分派请求与 ID，再发送 P1。接纳未知时 Task 保持 DISPATCHING；由于契约 ExecutionSummary 没有 UNKNOWN 枚举，未知请求只通过 current_execution_id 关联，得到确定状态后才加入 dispatches。
- GET 404、HTTP 超时或 5xx 都不触发另一笔执行。按原 ID、原载荷查询或重放，直到 P1 给出确定结果。
- REJECTED 不占尝试预算；可重试拒绝至少退避 1 秒。已接受 FAILED 且可重试才进入下一次尝试，总计不超过 3 次。
- 正常轮询约 1 秒；依赖异常按 1/2/4/8/10 秒退避，同一调度轮不重叠。JSON 连接/响应超时 2/10 秒，文件传输 60 秒；这些都不是执行失败判据。
- 准备请求也先持久化；包括 503 在内的未知接纳先按原 ID 对账，单次 GET 404 只重放原载荷。确定 REJECTED 后，可重试拒绝至少退避 1 秒再换 ID；准备和拒绝不计接受尝试。FAILED 或不可重试拒绝等待 P1 修复为 READY、更新会话或换可用节点。
- P1 成功后，校验结果的 SHA、结构、输入关联、尺寸、bbox。保存结果与 Task 成功在同一次 SQLite 事务完成；失败不合并假文本。
- 单个结果暂不可下载时保留同一执行，其他任务仍可推进。完成后的结果与 ZIP 从 P2 持久数据生成，不再依赖 P1。
- Job 取消期间继续对账未知请求，不新开尝试；已成功结果保留。P2 重启恢复原映射，不重新创建一批任务。
- SQLite 含原始分派映射和输入元信息，可供开发排查；ZIP 只导出公共状态和结果，不导出内部请求、租约、令牌或服务器路径。

## 代码归属与下一步真实联调

| 目录 | 内容 |
|---|---|
| task_service/ | HTTP 接口、配置、契约校验、P1 客户端、SQLite、调度与生命周期 |
| splitters/ | 固定 OCR 输入校验与逐图拆分 |
| aggregators/ | 顺序汇总与 ZIP 导出 |
| tests/tasks/ | 显式模拟 P1、故障用例、真实 HTTP 进程自测、合成样例 |
| contracts/ | 未修改的团队 OpenAPI 快照及来源说明 |
| docs/ | P1 交接清单与本次独立自测证据 |

真实联调所需的接口、配置、模型 profile、异常语义与验收顺序见 [P1 联调交接清单](docs/P1-联调交接清单.md)及 [统一联调入口](../qa/p1-p2/README.md)。本轮实施依据是 [统一计划 1.0](../P1-P2统一修改与联调计划.md)，公开契约已扩展为 0.4.0（CPU 多槽及输入预取）。P2 当前不包含原生 PDF、表格结构恢复、ASR、LLM、前端或模型执行器。
