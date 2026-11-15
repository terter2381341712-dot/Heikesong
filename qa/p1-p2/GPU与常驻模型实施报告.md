# GPU 接入与模型常驻：第一阶段实施报告

日期：2026-11-14。结论：第一阶段代码完成，本机 CPU 回归与真实联调通过；NVIDIA CUDA 路径的配置/拒绝降级/中心准入在本机模拟验证通过，**Windows GPU 实际推理、显存释放及物理跨机接续尚未验证**。

## 实现

- [完整分阶段计划](../../归档/P1-P2异构资源改造计划.md)；[CPU/Windows GPU 启动说明与实机验收清单](../../P1/docs/GPU与常驻模型启动.md)。
- 新增可选 `ocr.heterogeneous.v1`，CPU/CUDA 共用同一个冻结模型配置，分别锁依赖。保留原 CPU profile 和默认启动方式；旧 Job 不静默换 profile。
- GPU 节点明确选择 CUDA/device-id，检查全部三组模型实际首选 provider 和设备；禁止整会话失败后静默回退 CPU。中心按允许后端、provider、模型 SHA 和校准判断 READY/接单。
- 准备和连续任务共用常驻计算进程；只有取消、共享关闭、会话失效、心跳失联、硬截止、退出或崩溃时销毁。节点日志记录模型加载/PID/后端/设备/加载时间。
- 新内部 `runtime_stopped` 在确认进程退出后原子结束旧工作、撤销 READY 和会话；需要重新注册并准备。普通 Job 取消不重派；共享关闭继续携带原 NODE_UNAVAILABLE 标记供 P2 迁移；成功结果保留。
- P2 原有协议已可选择已验证 CUDA 能力，本阶段不改变调度策略，只增加混合后端与接续测试。仍为单节点单槽，不含多槽、自动后端选择、GPU 优先或显存预算调度。
- 新增 `node_agent.gpu_probe`，供 Windows 实机记录真实 OCR 自测与 Det/Cls/Rec 算子 provider trace；provider 字符串不代替实际 CUDA 算子证据。

## 本次实际运行的检查

| 检查 | 结果 | 证据 |
|---|---|---|
| P1 全量 pytest | 75 passed，0 skipped | [JUnit](gpu-resident-p1-pytest.xml) |
| P2 + 联调工具 pytest | 112 passed，0 skipped | [JUnit](gpu-resident-p2-pytest.xml) |
| 新 profile 的真实 CPU HTTP/OCR 闭环、常驻复用、服务重启恢复 | PASS，单 Mac | [原始记录](gpu-resident-real-result.json)；本地日志 var/check-78518ea6 |
| 共享关闭后的真实双节点接续 | PASS，同一 Mac 两个 Agent | [原始记录](gpu-resident-sharing-result.json)；本地日志 var/sharing-a7acd33c |
| 真实 RUNNING 任务取消、随后新 Job 重新准备 | PASS，单 Mac；没有重派取消任务，新进程完成新任务 | [原始记录](gpu-resident-cancel-result.json)；本地日志 var/resident-cancel-a1bb3f52 |
| P2/联调工具及新增核心文件静态检查 | PASS，使用项目 P2 Ruff 配置 / P1 E,F 检查 | 命令见下 |
| 三份公开契约一致性、新旧 profile schema 校验 | PASS | 三份 OpenAPI 字节相同，两个锁文件符合 RuntimeProfile |
| Windows CUDA / 两台物理设备 | NOT_RUN | 需按启动文档执行，不用模拟测试替代 |

P1 新增测试包括：多个任务同 PID/准备状态复用；取消/共享关闭/心跳失联/会话失效/退出/硬截止均在 ACK 前停止进程；进程崩溃；CUDA 三会话及 device-id 配置；provider 缺失、错误回退和设备错配拒绝；GPU 依赖分发包；新 profile 真实 CPU 模型加载；中心 CUDA 准入；停止确认的重放、旧会话隔离和共享退出错误标记。

P2 新增两项为模拟协议测试：同 profile 的 CPU/CUDA 混合节点分派，以及 CUDA 退出后转 CPU 且 profile 与尝试预算保留。不能将其计为 GPU 实测。

真实联调新增断言：一次准备仅记录一次 model_loaded，连续两张图片的 execution PID 均为该进程。另一条测试在第一张成功、第二张已派发后关闭 A，再由 B 完成第二张，第一张原 Task 不变，第二张 attempts_used=2。

## 复验命令

```sh
# 在 P1 目录
.venv/bin/python -B -m pytest tests/resource -q
# 在 P2 目录
.venv/bin/python -B -m pytest tests/tasks ../qa/p1-p2/test_run_local.py -q
# 在项目根目录
P2/.venv/bin/python -B qa/p1-p2/run_local.py check --profiles-dir P1/profiles/heterogeneous
P2/.venv/bin/python -B qa/p1-p2/check_sharing.py
P2/.venv/bin/python -B qa/p1-p2/check_resident_cancel.py
P2/.venv/bin/ruff check --config P2/pyproject.toml P2/tests/tasks/test_gpu_capability.py qa/p1-p2/run_local.py
P2/.venv/bin/ruff check --isolated --select E,F --ignore E501 P1/node_agent/compute.py P1/node_agent/gpu_probe.py P1/tests/resource/test_gpu_resident.py
```

P1 测试仍出现既有 LibreSSL/urllib3 提示。首次语法编译尝试因系统 Python 缓存目录不可写而失败，后续使用 `-B` 跑测试；异构目录首次联调发现启动器仍固定旧 profile_id，已改为按目录描述符核对并复验通过。未为通过测试放宽模型、结果或会话校验。

## 必须实机确认的交付边界

Windows Python 3.11.9 + ORT GPU 1.19.2 + CUDA 12.x/cuDNN 9.x 的兼容性；三模型真实 CUDA 算子与文字质量；GPU 常驻/显存占用与退出释放；GPU 活动任务取消与关闭迁移；Mac CPU 和 Windows GPU 同批贡献；故障恢复及真实吞吐。当前不承诺加速倍数，也不将同机双 Agent 当两台物理设备。

补充：在本机运行 GPU 诊断工具返回 FAIL/退出码 2，正确报告当前为 Python 3.9.6 的 CPU 环境，不能冒充 CUDA 验证通过。
