# CPU 多槽与线程预算：第二阶段实施报告

2026-11-14，本机 macOS / CPU 验证通过。P1 资源服务与 P2 任务服务使用独立 Python 环境，通过真实 HTTP 协作；两节点为同一台物理电脑上的独立 Agent，不能据此宣称已完成物理多机或 Windows GPU 验收。

## 已交付

- CPU 可配置多个槽，每槽一个独立常驻模型进程；`--slots / --threads / --cpu-budget` 校验贡献预算。ORT intra-op、inter-op、OpenCV 与常用数学库线程配置实际接入。
- P1 在接纳事务内绑定 slot_id；准备预留整节点，模型全部自测通过后开放容量。冷槽、隔离槽、取消待确认槽不能执行新任务。
- 单槽有独立 generation、停止确认和自测恢复。取消、崩溃、租约/硬期限不再使其他健康槽被整体重置。心跳仅续租仍存活进程的执行；旧心跳不能覆盖新会话槽代次。
- 整机关闭共享仍先停止并 join 全部进程，再原子撤销会话；NODE_UNAVAILABLE 的迁移标记保留，普通 Job 取消不迁移，已成功结果保留。
- P2 按剩余槽数派发，一轮可向同节点派多项任务；已在资源快照中的预留不重复扣减，尚待对账的请求保守扣减，成功但结果待下载不占计算槽。
- GPU 仍采用 `--device-id` 指定卡、单进程单槽；Agent 与注册接口拒绝 CUDA 多槽。CUDA provider/device 校验与第一阶段常驻流程保留。
- 契约同步升至 0.3.0，新增预算字段与 execution.slot_id，槽位上限扩到 64；P1 SQLite 自动增补结构，P2 数据库无需改动。

配置、内部接口字段与限制详见 [CPU 多槽与线程预算](../../P1/docs/CPU多槽与线程预算.md)。

## 自动化测试

| 检查 | 结果 | 证据 |
|---|---|---|
| P1 全部资源测试 | **92 passed** | [JUnit](cpu-multislot-p1-pytest.xml) |
| P2 任务测试 + 联调工具测试 | **117 passed** | [JUnit](cpu-multislot-p2-pytest.xml) |
| 真实 HTTP / OCR 两槽联调 | **PASS** | [完整记录](cpu-multislot-real-result.json) |
| 修改范围的 Ruff 静态检查、git diff --check | **PASS** | 本机执行，无格式/静态错误 |
| Windows NVIDIA GPU | **NOT_RUN** | 当前机器无 NVIDIA CUDA 设备 |

P1 新增 17 项测试，覆盖 5 个并发请求争抢 2 槽时仅 2 项接纳、槽认领隔离、ACCEPTED/RUNNING 取消、停止重放、晚到结果拒收、选择性续租、单槽租约隔离、全部共享关闭、预算拒绝、真实子进程 kill/join 顺序、ORT 实际会话线程参数、重新准备和迟到心跳。P2 新增 5 项覆盖批量填槽、已有预留不重复扣减、丢失接纳回包及重启对账、结果下载不占计算槽、外部占槽下的剩余容量。

P1 测试保留环境自带的 urllib3 / LibreSSL 警告；没有测试失败。CUDA provider 路径测试使用模拟会话，不能代替 GPU 实机证据。

复跑命令（项目根目录）：

```sh
(cd P1 && .venv/bin/python -B -m pytest tests/resource -q)
(cd P2 && .venv/bin/python -B -m pytest tests/tasks ../qa/p1-p2/test_run_local.py -q)
P2/.venv/bin/python -B qa/p1-p2/check_multislot.py
```

## 真实 OCR 观测

最终运行目录为 `qa/p1-p2/var/multislot-f1982434`，公开记录已复制到本目录。Agent A 配置为 2 槽 × 2 线程，总预算 4；Agent B 为 1 槽 × 2 线程。输入为真实生成的两张不同 3000×3000 文本图片及一张小文本图片，没有用 sleep 或假推理制造业务并发。

1. 两个 Job 的执行同时处于 RUNNING，Agent 日志确认已进入真实 OCR。对应 PID 为 **88753 / 88754**，slot_id 不同；资源快照为 max=2、reserved=2、available=0。
2. 取消第一个 Job 后只重建其槽，新增 PID **88852**；第二个 Job 由原 PID 完成，attempts_used=1，没有重载其模型。取消 Job 只有一次派发，不重新排队。
3. 后续两张图成功完成，A 的 model_loaded 仍只有三条，证明恢复后的槽与未受影响的槽持续复用模型。
4. 新批次先完成一张小图，另外两张大图同时 RUNNING 时关闭 A 共享，再开启 B。最终三项均 SUCCEEDED，尝试数为 **1 / 2 / 2**。首项 result_artifact 保持原值；两项迁移执行保留 NODE_UNAVAILABLE，并在不同 worker 完成。

该检查验证并发与生命周期正确性，不是吞吐基准。尚未测量代表性负载下 1/2/4 槽的吞吐、P95、内存峰值或加速比。

## 仍需实机确认的范围

- Windows NVIDIA：实际 CUDA 算子执行、指定 device_id、显存占用、连续任务模型复用；沿用 [第一阶段 GPU 启动与实机验证](../../P1/docs/GPU与常驻模型启动.md)。
- Windows CPU spawn 多槽：线程配置、单槽取消与进程退出、整机 off、重新开启；本机只验证了 macOS spawn。
- Windows GPU 单槽 + Mac CPU 多槽的物理跨机迁移、断网恢复与真实输入/结果传输。
- 资源边界：本阶段计算库线程预算不是 OS CPU 硬配额；内存/显存准入、同节点混合池、多 Agent 同卡协调、动态资源感知与公平调度仍待后续实施。
