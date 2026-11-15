# GPU 逻辑多槽改造计划（方案 B）

> 状态：**已实现**（本机代码 + 测试完成，P1 125 / P2 150 全绿；Windows GPU 实机验证与 0.5.0 契约发布待办，见文末「执行状态」）
> 契约目标版本：**0.5.0**（当前 0.4.0，版本升级属 P4 协调发布 T4.2，本次未动 OpenAPI）
> 变更范围：P1（节点与资源服务，主）+ P2（调度侧小幅适配）+ P4（契约与验收）
> 核心思想：把「槽 = 独立常驻进程 = 一份完整模型」改成「槽 = 同一常驻进程内的一个**逻辑推理路（logical lane）**」，模型权重只加载一份，取消/隔离细化到"路"级，用**显存预算**做准入。

---

## 改动边界与 CPU 隔离（硬约束）

**原则：GPU 多逻辑路与 CPU 多槽是两套独立执行模型、两套独立代码路径，互不修改。** CPU 的约束（每槽线程、线程预算、进程代次、整机准备占槽、预取仅单槽）原样保留；改动只在 GPU 后端分支上做"放宽/新增"，不做任何会收窄 CPU 行为的修改。

### 为什么不会波及 CPU 多槽（结构性隔离）

1. **执行模型分离**：CPU 多槽 = `node_agent/pool.py`（进程/槽 + 线程预算）；GPU 多路 = 新增 `node_agent/gpu_lanes.py`（进程内 lane）。不同文件、不同模型，无共享可变状态。
2. **槽台账后端无关**：`execution_slots(worker_id, slot_id, generation, ready, blocked)` 本身不含 backend，schema 不变。CPU 把 `slot_id` 当"OS 进程序号"，GPU 把 `slot_id` 当"逻辑路序号"，仅解释不同。
3. **校验是"分支放宽"，不是"改 CPU"**：CPU 相关校验（`threads_per_slot>=1`、`max_slots×threads≤budget≤逻辑核心`、`prefetch 仅单槽`）在 `agent.py` / `models.py` / `service.py` / P2 `candidate_action` 中统一加 `backend=="cpu"` 守卫，CPU 分支字节不变。
4. **P2 只删 GPU 专属拦截**：`candidate_action` 里只有一行 `cuda and max_slots != 1` 是 GPU 专属拦截，删除/替换它不影响 CPU 的 `remaining` 计算、线程预算校验与派发。
5. **预取不混入**：预取（`input_pipeline.py` / `prefetch.py`）仍仅单槽可用；GPU 多路阶段先 `prefetch=0`，不改预取路径，避免连带影响 CPU 预取。

### 最小改动点清单（仅 GPU 相关，CPU 分支不动）

| 文件 | 改动 | 是否触碰 CPU |
|---|---|---|
| `node_agent/agent.py` | `run()` 按 backend 分流：CPU 多槽→`run_pool`（不动）；GPU→新 `gpu_lanes` 入口 | CPU 分支不动 |
| `node_agent/gpu_lanes.py`（新增） | lane 池、共享 session、路级取消/隔离 | 无关 |
| `node_agent/agent.py` `__init__` | 放开 cuda 多槽；CPU 线程预算校验加 `backend=="cpu"` 守卫 | CPU 行为不变 |
| `resource_service/models.py` | `RegisterRequest` 放开 cuda 多槽（新增 GPU 显存预算字段与校验，CPU 校验分支不动）；`SlotReadyRequest.backend` 放宽为 cpu\|cuda | CPU 校验分支不动 |
| `resource_service/service.py` | `preparation_status` 去掉 `max_slots>1 and backend!=cpu` 拦截（GPU 改走 lane ready）；`node_claim`/`slot_ready` 按 backend 分流 | CPU 分支不动 |
| `P2/task_service/service.py` | 删除 `cuda and max_slots != 1` 拦截；线程预算块加 `backend=="cpu"` 守卫 | CPU 分支不动 |

---

## 0. 目标与预期成果

### 目标

在 GPU 节点上：一个常驻进程只 `load_locked_runner` 一次（一份权重），进程内起 N 个逻辑推理路，每路独立提交执行；取消/隔离从"杀整个进程"降为"取消/隔离某一路"；新增显存预算器做准入，替代现在的进程级隔离。槽台账 `execution_slots` 语义保持不变，只是 slot 不再 1:1 对应 OS 进程。

### 预期成果（可验证、可量化）

| 编号 | 成果 | 验证方式 |
|---|---|---|
| C1 | **一份权重常驻**：N 路共享同一份模型权重，显存不随路数线性翻倍（仅 session 上下文/激活增量） | 显存测量：权重 vs 每路增量 |
| C2 | **逻辑多槽**：GPU 节点可声明 1..N 个逻辑槽，契约升至 0.5.0，三份 OpenAPI 一致 | 契约校验 + 联调 |
| C3 | **路级取消/隔离**：正常取消只停一路，不杀整个模型进程，其他路继续 | 验收用例 |
| C4 | **吞吐可测量提升**：Windows GPU 实机给出单槽 vs 多路对照 | 压测报告 |
| C5 | **零回归**：CPU 行为、结果正确性（SHA/bbox/顺序）、取消/恢复/对账全部保持 | 全量回归 |

---

## 1. 里程碑与优先级

优先级：P0 = 最高（前置/阻断），P1 = 高，P2 = 中。
时间按**相对工期**标注（映射到团队日历自行换算），以"退出标准"为准，不以日历日期为准。

| 里程碑 | 内容 | 优先级 | 退出标准 |
|---|---|---|---|
| M0 | 基线与现状冻结 | P0 | gpu_probe/benchmark 基线产出；正确性回归全绿 |
| M1 | 进程内逻辑路跑通（B2 共享 session） | P0 | 单进程 N 路并发、结果正确、一份权重证据 |
| M2 | 显存预算 + 契约 0.5.0 发布 | P1 | GPU 逻辑槽可声明/校验；OpenAPI 三份一致 |
| M3 | P2 端到端调度（真实 GPU） | P1 | P2 按剩余逻辑槽派发，端到端闭环 |
| M4 | 实机压测 + 回归 + 报告 | P0 | 吞吐/显存/取消证据齐备，验收通过 |

---

## 2. 分阶段执行清单

> 每项标注：编号 / 任务 / 负责角色 / 交付物 / 验收标准。
> 角色：**P1**（节点 + 资源服务 + 执行器 + profile）、**P2**（任务服务调度）、**P4**（契约 + 联调 + 验收）。成员姓名待补。

### Phase 0 · 基线与现状冻结（P1 + P4）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T0.1 | Windows GPU 产出单槽基线：图片/秒、P50/P95、GPU 利用率、VRAM 峰值、每 session 显存 | P1 | 基线报告（JSONL/MD） | 数据可复现、同参数可对照 |
| T0.2 | 调研 RapidOCR 三模型 ONNX 是否带动态 batch 轴；ORT 同 session 并发 Run 行为与显存占用 | P1 | 调研记录 | 明确 B1/B2 可行性依据 |
| T0.3 | 全量跑 P1/P2 回归 + `run_local.py check`，冻结正确性基线 | P4 | pytest XML + check report | 全绿 |
| T0.4 | 冻结 0.4.0 快照与三份 OpenAPI 哈希 | P4 | 快照记录 | 哈希一致 |

### Phase 1 · 「槽=进程」抽象为「槽=逻辑路」（P1，内部重构，不动契约）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T1.1 | 在 `node_agent` 新增 `gpu_lanes.py`，实现 `Lane`/`LanePool` 抽象：lane_id、generation、ready/blocked、当前 execution（**不改 `pool.py` 的 CPU 进程池**） | P1 | 代码 | 单测覆盖 lane 状态机 |
| T1.2 | 单进程 resident 模型子进程承载多条 lane（进程内线程/协程，共享一个 runner） | P1 | 代码 | 多 lane 并发执行不崩 |
| T1.3 | lane 提交/结果回传按 `execution_id` 隔离，保持 `ExecutionComplete` 语义 | P1 | 代码 + 单测 | 结果不串路 |
| T1.4 | 保留「整进程退出 → `runtime_stopped`」作为灾难兜底 | P1 | 代码 | 进程退出仍能撤销会话 |

### Phase 2 · 单 session 共享 + N 路并发（核心，P1）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T2.1 | 实现 **B2 子方案**（仅 GPU 路径）：一个 InferenceSession + 锁串行提交 + intra-op 线程并行（真·一份权重），**不改 CPU 的进程级多槽推理** | P1 | 代码 | 权重显存只有一份 |
| T2.2 | 测量 N 路下 intra-op 线程 × 路数是否过订阅，定每路线程预算 | P1 | 测量记录 | 无过订阅 |
| T2.3 | （可选）吞吐不足时演进 **B1**：每路独立 session + 显存预算，做成开关 | P1 | 代码 + 开关 | 开关可切换、可对照 |
| T2.4 | 单元/集成测试：N 路并发结果正确、无数据竞争、取消单路不误伤 | P1 | 测试 | 全绿 |

### Phase 3 · 显存预算 + P1 契约（P1：models/slots/service）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T3.1 | 扩展 `RegisterRequest`：GPU 可声明 `max_slots`（逻辑路）+ 每路显存 + 显存预算；移除「GPU 必须单槽」校验（`models.py`、`agent.py` 的 `backend=="cuda" and slots!=1`） | P1 | 代码 | GPU 多槽可注册 |
| T3.2 | `execution_slots` 语义：slot → 逻辑路，保留 generation/ready/blocked | P1 | 代码 + 迁移 | 旧库兼容 |
| T3.3 | 准入：P1 校验 `max_slots × 每路显存 ≤ 可用显存 × 安全系数`，动态控制可接单路数 | P1 | 代码 | 超预算拒绝接单 |
| T3.4 | 扩展 `SlotReadyRequest`/`slot_ready` 允许 `backend=cuda`；`node_claim` 支持 GPU 按 lane 领取（`preparation_status` 中 `max_slots>1 and backend!=cpu` 的限制同步放开） | P1 | 代码 | GPU 多路可 ready/claim |
| T3.5 | 单测：注册校验、预算边界、claim/ready/stopped 重放与代次防护 | P1 | 测试 | 全绿 |

### Phase 4 · P2 适配 + 契约升级（P2 + P4）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T4.1 | 移除 `candidate_action` 中「cuda 必须单槽」限制，按剩余逻辑槽派发（`P2/task_service/service.py`） | P2 | 代码 | GPU 多槽可被调度 |
| T4.2 | P4 发布 0.5.0 OpenAPI（三份一致）、更新 examples / task-types / acceptance-cases | P4 | 契约文件 | 三份哈希一致 |
| T4.3 | P2 回归 + 联调（模拟 P1 多槽 GPU 能力） | P2 | 测试 | 全绿 |

### Phase 5 · 路级取消/隔离/恢复语义（P1 + P4 验收）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T5.1 | 取消一路：只终止该路推理、标 CANCELLED，其他路继续；重放/代次防晚到覆盖 | P1 | 代码 | 单路取消不误伤 |
| T5.2 | 单路崩溃隔离：捕获单路异常、该路 blocked/重建，不拖垮进程（尽力而为） | P1 | 代码 | 单路异常可控 |
| T5.3 | 进程级崩溃兜底 + 重启恢复：`runtime_stopped`、会话撤销、未终态 FAILED | P1 | 代码 | 进程崩溃仍收敛 |
| T5.4 | P4 验收：取消/隔离/恢复场景用例 | P4 | 验收记录 | 用例通过 |

### Phase 6 · Windows GPU 实机验证 + 压测 + 回归（P4 + P1）

| 编号 | 任务 | 角色 | 交付物 | 验收标准 |
|---|---|---|---|---|
| T6.1 | Windows GPU 实机跑 N=1/2/4 对照压测，记录吞吐/显存/利用率 | P4 | 压测报告 | 有对照数据 |
| T6.2 | 全量回归（P1 + P2 + `run_local.py check`） | P4 | pytest XML | 全绿 |
| T6.3 | 更新实施报告、验收矩阵状态、README 版本说明 | P4 | 文档 | 版本与状态一致 |

---

## 3. 资源与依赖

### 3.1 环境
- **Windows NVIDIA GPU 机器**：CUDA 12.x + cuDNN 9.x + Python 3.11.9 + `onnxruntime-gpu==1.19.2` + `rapidocr==3.10.0`（GPU 独立环境）。
- **Mac CPU 机器**：逻辑路逻辑可先在本机 CPU 验证（不测 GPU 算子），降低对 Windows 的阻塞。
- 前置：Windows GPU 实机可达，`profiles/heterogeneous` 自测通过（当前仍"待实机验收"）。

### 3.2 关键前置决策：B1 / B2 子方案选型
- **B2（共享单 session + 锁 + intra-op 并行）**：真·一份权重、显存最小，但提交串行，并行度依赖 intra-op。
- **B1（每路独立 session）**：每路一份权重副本、显存 N 倍，但真并行。
- **建议**：先落地 B2（M1/M2），用 M4 实测决定是否打开 B1 开关。二者都写进代码，做成可切换配置。

### 3.3 依赖项与代码归属
- P1 拥有：`node_agent/{agent,compute,pool}.py`、`resource_service/{models,slots,service}.py`、`runners/rapidocr_runner.py`、`profiles/`。
- P2 拥有：`task_service/service.py` 的 `candidate_action`（放开 cuda 单槽限制）。
- P4 拥有：`contracts/openapi.yaml` 三份副本、examples/acceptance-cases、联调与验收的唯一发布权。

---

## 4. 关键风险与缓解措施

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| R1 单进程崩溃放大：N 路都在一个进程，OOM/段错误带崩所有路 | 高 | 显存预算保守 + 单路异常隔离；保留"整进程崩溃→全槽失效"兜底；必要时退化为"少量进程、每进程少量路"混合 |
| R2 显存预算不准导致 OOM | 高 | 开机实测每 session/每路显存 + 峰值，设安全系数与系统保留；准入以实测为准 |
| R3 并发过订阅：intra-op 线程 × 路数超配 | 中 | 每路降 intra-op 线程、总量预算、同 session 锁串行提交 |
| R4 并发导致正确性/顺序回归 | 高 | 结果按 `execution_id` 隔离、P1 SQLite 事务保证；全量回归 |
| R5 Windows 差异：MPS 不可用、spawn、文件句柄 | 中 | B 方案不依赖 MPS；spawn 语义已有处理；实机覆盖 Windows |
| R6 契约 0.5.0 破坏兼容 | 中 | P4 统一发布、三份同步、样例 + 验收同步更新 |
| R7 吞吐提升不明显或为负 | 中 | 先基线、同数据同参数对照、不预设倍数；负收益则回退或打开 B1 开关 |

---

## 5. 衡量指标（KPI）

| 维度 | 指标 |
|---|---|
| 正确性 | 全量回归（P1 + P2 + `run_local.py check`）通过；结果 SHA/bbox/顺序与单槽一致 |
| 吞吐 | Windows GPU 实机：图片/秒、整批 wall time、P50/P95、GPU 利用率、VRAM 峰值（N=1/2/4 对照） |
| 资源 | 每 session/每路显存、权重显存占比；证明显存不随 N 线性翻倍（B2）或给出 N× 关系（B1） |
| 可靠性 | 路级取消/隔离、单路崩溃不误伤、进程崩溃兜底、重启恢复 |
| 契约 | 0.5.0 三份 OpenAPI 哈希一致、acceptance-cases 状态更新、examples 有效 |

---

## 6. 交付物总表

| 类别 | 交付物 | 产出阶段 |
|---|---|---|
| 基线 | Windows GPU 单槽基线报告、调研记录、正确性回归快照 | Phase 0 |
| 代码 | `Lane`/`LanePool` 抽象、共享 session 并发执行、B1 开关、显存预算器、路级取消/隔离 | Phase 1–3, 5 |
| 契约 | 0.5.0 OpenAPI ×3、examples、task-types、acceptance-cases 更新 | Phase 4 |
| 测试 | lane 单测、预算边界测试、P2 调度测试、取消/恢复验收用例 | Phase 2–5 |
| 报告 | Windows GPU 压测报告、全量回归结果、实施报告与 README 版本说明 | Phase 6 |

---

## 执行状态（2026-11-14 更新）

### 已完成（本机代码 + 测试）

- **Phase 1–2**：新增 `P1/node_agent/gpu_lanes.py`（`LanePool`/`Lane` + `run_gpu_lanes`），`P1/node_agent/compute.py` 新增 `compute_lanes` 子进程入口（一份权重 + N 线程共享一个引擎锁，B2 方案）；`agent.py` 按 backend 分流（CPU 多槽→`pool.py` 不动，GPU 多路→`gpu_lanes.py`）。
- **Phase 3**：`models.py` 放开 cuda 多槽并新增 `gpu_lane_vram_bytes` 显存预算校验；`service.py` 去掉 `preparation_status` 的「GPU 多槽拦截」。
- **Phase 4（代码）**：`P2/task_service/service.py` 删除 cuda 单槽拦截，CPU 线程预算加 `backend` 守卫（预取校验仍对全后端生效）。
- **Phase 5**：路级取消 = `stop_lane` + 父进程上报 `slots/{slot_id}/stopped`，该路晚到结果被 P1 `STALE_EXECUTION` 拒收；整进程退出仍走 `runtime_stopped` 兜底。

### 工程改进轮修复（2026-11-14 第二轮）

- **F1（严重·已修）准备阶段死锁**：整机 READY 的触发条件原为 `all(lane.ready)`，但准备期间能力仍是 PREPARING、逐槽 `slots/{slot_id}/ready` 必然 409，导致 GPU 多路节点永远不就绪。改为：共享自测证据就绪即上报整机 READY（服务端一次性把全部槽置 ready=1），本地同步 lanes 就绪。
- **F2（高·已修）路级取消后自动恢复**：`_report_stopped` 轮换代次后，由恢复循环复用常驻模型的同一份证据重报该路 ready。
- **F3（高·已修）子进程崩溃检测**：`tick()` 检测共享子进程存活；`_send` 发送加固（管道失败不再击穿 tick）；`stop()` 对 child/pipe 分离做健壮处理；崩溃走整机 `runtime_stopped` 兜底。
- **F4（中·已修）每路显存测量工具**：`gpu_probe.py` 新增 `nvidia_mem_used_bytes`，在生产 runner 三 session 加载前后测显存增量，报告新增 `lane_vram_estimate_bytes`；节点 CLI/报错提示用 gpu_probe 获取该值。
- **测试**：`P1/tests/resource/test_gpu_lanes.py` 增至 14 项（含准备门控、取消恢复、子进程崩溃、显存查询、父子协议冒烟）。**回归全绿：P1 130 passed，P2 150 passed；run_local check 7 项 PASS；check_multislot/sharing/resource_scheduling/resident_cancel 全 PASS**。

### 待办（需外部条件 / 协调，非本机可完成）

1. **Phase 0 / Phase 6 · Windows GPU 实机验证**：`gpu_probe` + 单槽 vs 多路（N=1/2/4）压测、显存峰值、真实 CUDA 算子落点、吞吐对照——需一台 Windows NVIDIA GPU 机器。
2. **T4.2 · 契约 0.5.0 发布**：本次功能改动**未新增公共字段**，故未动 OpenAPI；若团队决定立即升版，需 P4 统一改三份 `openapi.yaml`（`version` + 多处 `const: 0.4.0`）及 `config.py`/`app.py`/`aggregators`/`mock_p1`/`verify_p1`/相关测试的版本断言。
3. **B1 真并行 GPU 推理（评估后推迟）**：B2 共享 session + 引擎锁使 GPU 推理串行，多路只重叠 CPU 下载/解码；真并行需 B1（每路独立 session，N× 显存）或 batching，两者都需 Windows GPU 实测收益后才能安全决策，避免盲目改架构。
4. **每路显存实测**：`lane_vram_estimate_bytes` 已在 gpu_probe 提供测量通道，但真实数值需在 GPU 环境跑一次 gpu_probe 取得。
