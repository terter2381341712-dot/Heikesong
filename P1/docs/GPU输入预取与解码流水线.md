# GPU 输入预取与解码流水线（第四阶段）

本阶段在 `zyc` 实现输入预取和 CPU 解码与当前推理重叠。GPU 仍为单卡单槽，一图一执行；CPU 多槽沿用第二阶段实现。Windows CUDA 的真实算子落点、吞吐增益及显存释放仍须实机验证。本机证据见 [实施报告](../../qa/p1-p2/GPU输入流水线实施报告.md)。

## 启动与预算

先按 [GPU 环境说明](GPU与常驻模型启动.md) 安装冻结依赖、运行 `gpu_probe`，并使用允许 CPU/CUDA 的 `profiles/heterogeneous`。在 P1 目录启动 Windows 节点：

```powershell
python -m node_agent --server http://<P1地址>:8001 --name windows-gpu --backend cuda --device-id 0 --slots 1 --threads 2 --cpu-budget 3 --prefetch 1 --interactive
```

`on` 开启共享，`off` 停止共享。CPU/GPU 模型仍在独立进程常驻；额外一个常驻 CPU 进程负责下一张输入的 HTTP 下载、校验、EXIF 纠向和 RGB 解码，不加载 OCR/CUDA 模型。相邻任务正常完成后两个进程都可以复用；提示撤销、失效或失败恢复时，解码进程可能重建。

- `--prefetch` 可选 0/1：CPU/CUDA 均默认 0，需显式开启；`--prefetch 0` 用于关闭与基线对照。
- 预取只允许 `--slots 1`，且 `threads + 1 <= cpu-budget <= 逻辑核心数`。未指定 threads 时，为推理选择最多 2 个线程，至少为预取保留 1 个核心预算；不足两个逻辑核心时应关闭预取。预算是计算库并行度配置，不是操作系统硬配额。
- Mac/Windows CPU 单槽可显式 `--backend cpu --threads 1 --cpu-budget 2 --prefetch 1` 验证同一输入流水线；CPU 多槽不启用预取。
- 缓存只容纳一个未来输入，压缩文件最大 10 MiB、像素最大 2000 万，RGB 数组最大 60,000,000 字节。执行消费时将缓存移入自身工作目录，下一张缓存才可开始生成。每次预取使用独立临时目录，防止异常退出遗留的下载覆盖新请求。当前执行数组、Pillow 临时副本、校验缓冲及模型另占内存；这不是整个节点的内存硬上限。
- 每次下载读取块有大小检查和网络超时；父进程对单次准备设 20 秒上限，可终止卡住的下载/解码进程。准备未完成、失败或本地缓存损坏时，执行停止相关准备并回到普通下载/解码路径，不为预取增加失败尝试。

## 调用与数据流

1. P2 先按现有能力、剩余槽位和历史耗时派发任务，再向启用预取的忙节点提示一张尚未分派的兼容图片。
2. 提示只包含 job/task/profile/artifact 身份，不创建 execution、不占槽、不增加 attempts，也不绑定任务到 GPU；其他空闲 CPU 可以正常领取这张图片。
3. Agent 在模型执行期间以约 250 ms 周期读取提示，独立 CPU 进程下载输入，核对 SHA/类型/尺寸后完成解码，将数组保存为禁用 pickle 的 `.npy` 文件。没有跨图片合批、CUDA stream 或显存预加载。
4. 下一次正常认领 execution 后，Agent 核对 job/task/artifact/SHA/profile，命中时移动缓存，并由模型进程校验缓存 SHA、数组类型和形状，直接调用 `RapidOCRRunner.execute_array()`。缓存未命中仍执行完整正常路径。普通路径也拆分了解码与推理，去掉原先下载校验中的重复完整解码。
5. P2 的资源历史键增加 `prefetch_capacity`，避免混用开启/关闭预取后的耗时；仍按实际剩余执行槽派单，不为缓存增加优先权。

## 接口和持久化

公开契约升级为 **0.4.0**，三个 OpenAPI 副本相同。P1、P2、Agent 和使用严格契约校验的客户端须同步升级；profile 锁文件及模型哈希保持原值。

| 接口/字段 | 语义 |
|---|---|
| 内部节点注册 `prefetch_capacity` | 默认 0；仅 0/1；中心验证单槽及额外 CPU 预算 |
| ResourceNode `prefetch_capacity` / `prefetch_revision` | 公布节点是否可预取及当前单调修订号；`available_slots` 语义不变 |
| `POST /api/resource/v1/resources/{worker_id}/prefetch` | TASK_TOKEN；`worker_session_id`、递增 `revision`、`hint` 或 null。hint 含 job_id/task_id/task_type/profile_id/profile_digest/input_artifact_id。返回节点、会话、修订号、过期时间 |
| 同修订号重放 | 相同内容可刷新 15 秒 TTL；内容不同或旧修订号返回 409，撤销后的晚到 PUT 不能复活旧提示 |
| `POST /api/resource/internal/v1/nodes/{worker_id}/prefetch` | session_id/node_secret 鉴权；返回有效提示（含输入 SHA、大小、类型、尺寸、期限）、本节点已接纳/运行任务身份及 enabled；不续 execution 租约 |
| `runtime_stopped` | Agent 确认模型及解码进程均已退出后才调用；保持原子撤销 READY、会话隔离及迁移语义 |
| ExecutionTimings | 新增可选 decode_ms、prefetch_hit、prefetch_download_ms、prefetch_decode_ms。原五项仍必填 |

P1 自动新增节点列及 `input_prefetches` 表；P2 新增同名表，保存希望发布的提示/撤销及修订号，先落库再发送。P2 重启可重放丢响应的撤销。无需手动重建数据库。

`download_ms`、`decode_ms` 表示当前执行真正等待的下载和解码/缓存载入；`inference_ms` 是 runner 数组调用（含其后处理）；`total_worker_ms` 为这些关键路径阶段之和，不包含 P2 排队、领取轮询或结果提交 HTTP。`prefetch_*` 只记录已重叠的背景成本，不加入 total，以免重复计时。开启预取后的节点领取间隔为 100 ms，未开启仍为 1 s；P2 的轮询/对账开销仍会影响整批吞吐。比较预取开关时需同时报告该行为差异及实际重叠区间。

## 取消、共享关闭、迁移

- 等待任务取消：P2 撤销提示；Agent 终止对应解码进程并清理缓存，正在执行其他任务的模型不受影响。P2 不可达时提示最长保留至 TTL；节点网络心跳超时仍按原规则处理。
- 当前执行取消/共享关闭/退出/会话失效/心跳超时/硬截止：父进程终止并 join 模型和解码进程，清理缓存，再确认停止。普通 Job 取消不迁移；共享关闭继续以 NODE_UNAVAILABLE 标记未完成执行，P2 按原尝试预算迁移。
- P2 已接纳下一张执行后可能立即更换提示。内部响应携带 owned_tasks，Agent 在认领前保留已接纳任务对应的缓存，避免被下一条提示提前清除；已请求取消的任务不属于 owned_tasks。
- 已成功提交的结果不因共享关闭被覆盖或重复执行。预取没有产生 execution，迁移到其他节点的预取任务不额外消耗尝试。

## Windows GPU 实机验收（待执行）

1. 冻结 profile 自测通过，三个 ORT 模型首选 CUDAExecutionProvider，设备编号正确；用 gpu_probe/ORT profiling 核对真实 GPU 算子，不能仅看 GPU 名称或 provider 字段。
2. 相同数据集、GPU、模型、推理线程数和 CPU 总预算，对照 `--prefetch 0/1`，区分冷启动与预热。记录正确图片/秒、批次总耗时、P50/P95、缓存命中率、CPU/RAM/VRAM 峰值、网络字节；不得预设提速比例。小图及高速局域网下可能没有收益。
3. 日志中的 prefetch_ready 起止区间与前一 execution 的推理区间重叠，且只有一个 GPU 模型执行在运行；连续任务 PID 与模型加载次数证明复用。
4. 覆盖 PNG/JPEG/EXIF、大小图片、慢网、缓存未命中/损坏、预取进程崩溃；结果文本、框、顺序及 SHA 校验与关闭预取一致，数组磁盘复制和额外内存成本在可接受范围。
5. 推理中取消与关闭共享：两个进程确认退出后才释放会话，Windows 文件句柄允许缓存清理，显存释放；旧回包被拒，已完成结果保留，未完成任务能迁移到 Mac CPU。
6. 单独取消下一张预取任务所属 Job，另一 Job 的当前 GPU 执行继续；断网/P2 重启/旧提示晚到不会重复执行或多扣尝试。
