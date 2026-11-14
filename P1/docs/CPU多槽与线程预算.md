# CPU 多槽与线程预算（契约 0.3.0）

CPU 节点现在可以同时处理多张图片，每个槽使用独立的常驻模型进程。CUDA 节点继续使用指定 GPU 上的一个常驻进程、一个执行槽。Mac 可以使用 CPU；是否使用 CUDA 由实际运行时和模型自测决定，不能只根据 Windows/macOS 判断。

## 启动

中心、P2、节点请一起升级到本次代码。原 CPU profile 的模型锁文件与 digest 不变；CPU/CUDA 混合集群仍按 [GPU 启动说明](GPU与常驻模型启动.md) 选择相同的 heterogeneous profile。

在 P1 目录、按原说明设置 JOIN_TOKEN、NODE_PROFILES_DIR 和独立 NODE_AGENT_DATA_DIR 后：

```sh
# 2 个常驻 CPU 槽，每槽 2 个计算线程，总预算 4 个逻辑核心。
.venv/bin/python -m node_agent --server http://中心IP:8001 --name mac-cpu \
  --backend cpu --slots 2 --threads 2 --cpu-budget 4 --interactive

# 每槽线程自动取 floor(cpu-budget / slots)，这里得到 2。
.venv/bin/python -m node_agent --server http://中心IP:8001 --name cpu-worker \
  --backend cpu --slots 3 --cpu-budget 6 --interactive
```

Windows CUDA 环境沿用第一阶段安装方式：

```powershell
.venv-gpu\Scripts\python.exe -m node_agent --server http://中心IP:8001 --name windows-gpu --backend cuda --device-id 0 --slots 1 --threads 2 --interactive
```

输入 `on` 开启共享，`off` 停止全部槽并迁移剩余任务。不要为同一张 GPU 启动多个独立节点实例；本阶段没有跨 Agent 的 GPU 设备配额服务，也不支持一个 Agent 同时运行 CPU 池与 GPU 池。

## 配置规则与边界

- 默认 `--slots 1`，保持原有启动方式。CPU 多槽显式开启，允许 1–64 槽。
- CPU 多槽要求每槽线程数至少 1，且 `slots × threads ≤ cpu-budget ≤ os.cpu_count()`。未给预算时使用检测的逻辑核心数，未给线程数时向下均分，预算不足直接拒绝启动。注册接口重复校验这些约束。
- 每个 ORT 会话的 intra-op 线程数为 `threads`、inter-op 为 1；子进程启动前设置 OpenMP、OpenBLAS、MKL、Accelerate、NumExpr 环境变量，子进程内设置 OpenCV 线程数。
- 这是计算库的并行度预算，**不是操作系统线程总数或 CPU 利用率的硬上限**。三个 OCR 模型会话、通信、心跳等仍有各自线程；其他应用使用的 CPU 不受本程序控制。
- 每槽各有一份模型，会增加内存占用。本阶段不做动态内存准入、基于实时 CPU% 的自动扩缩槽，也不承诺槽数翻倍就吞吐翻倍。需在目标机器上结合内存与真实图片测量后选槽数。
- GPU 配置 `--slots > 1` 会被 Agent 和 P1 注册接口拒绝，原 CUDA 实际 provider/device 校验继续有效。

## 接口与容量

公开 OpenAPI 升到 **0.3.0**。P1、P2、执行包内三份 OpenAPI 保持一致；旧 0.2 客户端把槽位上限写死为 1，必须同步升级。

| 接口/对象 | 新行为 |
|---|---|
| `POST /api/resource/internal/v1/nodes/register` | 增加 `max_slots`、`threads_per_slot`、`cpu_thread_budget`、`backend`；返回每槽 `slot_id/generation/ready/blocked` |
| `GET /api/resource/v1/resources` | `max_slots` 为配置值，`reserved_slots` 统计已接纳/运行/取消待确认的执行；准备期间预留整台节点。`available_slots` 仅计已准备、未被占用、未隔离的槽；冷节点空闲容量可用于触发准备 |
| `ResourceNode` | 新增 `threads_per_slot`、`cpu_thread_budget`；槽数和 active_execution_ids 上限从 1 改为 64 |
| `Execution` | 新增可空 `slot_id`；新接纳执行明确分配槽位，历史记录/拒绝记录可为空 |
| `POST .../nodes/{worker}/claim` | CPU 多槽执行认领需 `slot_id + generation`；不带槽的请求只取节点级准备，准备认领丢回包可重复取回 |
| `POST .../nodes/{worker}/heartbeat` | CPU 多槽上报 `running_execution_ids`，只续租本会话中仍 RUNNING、未过期的这些执行；返回槽快照和取消列表 |
| `POST .../nodes/{worker}/slots/{slot}/stopped` | 进程退出并 join 后提交旧 generation；原子结束该槽任务、轮换 generation、设为未准备；重放旧 generation 不影响替代进程 |
| `POST .../nodes/{worker}/slots/{slot}/ready` | 替代进程通过同 profile/backend/provider/model SHA 自测，携带新 generation 与 calibration 后恢复单槽容量 |
| `POST .../nodes/{worker}/runtime_stopped` | 保留整机停止确认：全部进程退出后才能调用，撤销会话与全部能力 |

P2 以节点快照的 `min(available_slots, max_slots-reserved_slots)` 为上限，再扣除尚未出现在快照中的待对账接纳请求。一轮内每次派发扣一槽，准备占整节点；已在 active_execution_ids 中的请求不重复扣槽，成功但结果暂未取回的执行不继续占计算槽。最终准入仍由 P1 的 SQLite 事务原子仲裁。

## 取消、关闭共享与恢复

单任务取消仅停止其所在进程；该槽确认停止、模型重新加载并自测后才重新开放。其他槽继续执行，保留 PID 和模型。已接纳但尚未认领的取消同样按所属槽处理。CPU 多槽不能再通过旧 `cancel_ack` 绕过槽代次确认。

单槽进程崩溃、硬期限或租约过期只隔离该槽；心跳不会给已死亡进程的任务续租。旧 generation 的 ready/stop 重放和旧执行的晚到结果不能覆盖替代任务。

关闭共享、会话失效、心跳失联与退出会停止并等待全部子进程退出。关闭共享的未完成执行保留 `NODE_UNAVAILABLE + retryable=true`，P2 按原规则迁移；普通 Job 取消不迁移；已成功结果不丢弃。再次开启共享需要重新准备模型。

P1 SQLite 启动时自动添加节点预算字段、executions.slot_id 与 execution_slots 表；原有历史执行保留，P1 重启仍撤销旧会话并按原规则收敛未完成工作。P2 数据库结构不变。

测试命令、结果及 Windows 待验项见 [第二阶段报告](../../qa/p1-p2/CPU多槽实施报告.md)。
