> 第四阶段已增加输入预取，CPU/CUDA 默认关闭，需显式开启；额外 CPU 预算与关闭方法见 [输入流水线说明](GPU输入预取与解码流水线.md)。以下保留第一阶段环境安装与设备核验步骤。

# NVIDIA GPU 与常驻模型：第一阶段使用说明

实现和验收边界见 [改造计划](../../归档/P1-P2异构资源改造计划.md)、[本机测试报告](../../qa/p1-p2/GPU与常驻模型实施报告.md)。CPU 多槽已在第二阶段实现，见 [多槽使用说明](CPU多槽与线程预算.md)；GPU 仍为单卡单槽。CUDA 环境不兼容会明确准备失败，不静默转 CPU。

## 选择模型目录

原 `profiles/ocr.default.v1.lock.json` 没有变更，旧 CPU 演示可继续运行。要在同一 Job 混用 Mac CPU 与 Windows CUDA，中心和全部节点都选择随仓库发布的 `profiles/heterogeneous`，并从 P2 的 `/api/task/v1/task-types` 读取新的 profile_id/digest。不要复制旧 Job 的 digest，不要在运行中的 Job 上替换目录。

在 Mac 的 P1 目录中，沿用 README 的 Python 3.12.15 CPU 环境与令牌配置：

```sh
export RESOURCE_PROFILES_DIR="$PWD/profiles/heterogeneous"
export NODE_PROFILES_DIR="$PWD/profiles/heterogeneous"
# 中心：DEMO_TOKEN、TASK_TOKEN、JOIN_TOKEN 按原说明设置
.venv/bin/python -m resource_service
# 在另一个终端，设置相同 NODE_PROFILES_DIR、JOIN_TOKEN 和独立 NODE_AGENT_DATA_DIR
.venv/bin/python -m node_agent --server http://中心IP:8001 --name mac-cpu --backend cpu --threads 2 --interactive
```

输入 on 开始共享，off 停止并释放常驻模型；下次 on 重新准备。P2 的启动方式不变，RESOURCE_BASE_URL/TASK_TOKEN 指向相同中心。仅节点需要 GPU 运行环境，P2 不加载模型。

## Windows NVIDIA 环境

以下 PowerShell 命令在项目 P1 目录执行。先安装并核实 **Python 3.12.15**、兼容 NVIDIA 驱动、**CUDA 12.x 和 cuDNN 9.x**；DLL 必须可被进程发现。`nvidia-smi` 显示的 CUDA 版本表示驱动能力，不等于所需 CUDA/cuDNN DLL 已安装。

```powershell
py -3.9 --version  # 必须与锁文件中的完整版本 3.12.15 一致
py -3.9 -m venv .venv-gpu
.\.venv-gpu\Scripts\python.exe -m pip install -r requirements-gpu.lock.txt
.\.venv-gpu\Scripts\python.exe -m node_agent.gpu_probe --device-id 0 --output-dir .\var\gpu-probe
```

使用干净 GPU 环境，不要在其中额外安装 `requirements.txt`（那会安装 CPU ORT）。如果选择其他 Python/ORT 版本，应正式发布新依赖锁并重新验证，不直接删除版本校验。

`gpu_probe` 的 PASS 要求锁文件/权重核验、三张真实 OCR 自测、Det/Cls/Rec 三组模型均有 CUDA 算子执行证据。输出 gpu-report.json 和三个 ORT trace；CPU 图分区会如实记录。此工具会额外创建带 profiling 的会话，仅用于诊断，不代表正式吞吐，也不证明多机接续已通过。

随后启动 GPU 节点：

```powershell
$env:NODE_PROFILES_DIR = (Resolve-Path .\profiles\heterogeneous).Path
$env:NODE_AGENT_DATA_DIR = Join-Path $PWD "var\windows-gpu-node"
# JOIN_TOKEN 设置为中心配置的加入令牌，不放入命令行或截图
.\.venv-gpu\Scripts\python.exe -m node_agent --server http://中心IP:8001 --name windows-gpu --backend cuda --device-id 0 --interactive
```

输入 on 后，提交真实图片 Job，P2 自动准备。应看到 capability 为 backend=cuda、runtime_provider=CUDAExecutionProvider、verified=true，且模型 SHA 与目录一致。连续图片日志应复用同一个 model_loaded PID；取消/关闭后进程结束，再准备时出现新的 PID。

第一阶段一台节点显式使用一种后端；同一 GPU 不建议启动多个 Agent 竞争显存。多槽、CPU/GPU 混合池和显存预算调度属于后续阶段。

## 本机复验（项目根目录）

```sh
P2/.venv/bin/python qa/p1-p2/run_local.py check --profiles-dir P1/profiles/heterogeneous
P2/.venv/bin/python qa/p1-p2/check_sharing.py
```

第一条测试真实 CPU OCR、模型常驻及服务重启等路径；第二条测试同机两个节点的共享关闭接续。两者均不能代替 Windows GPU 或两台物理机器验收。

## 必须在 Windows/物理设备补做

1. CUDA DLL、驱动和三模型加载；gpu_probe 的真实算子落点和文本自测。
2. 从 P2 分派到 Windows GPU，完成真实输入、结果 SHA/结构校验和 ZIP；连续执行确认常驻。
3. 一批不同输入由 Mac CPU 与 Windows GPU 都产出最终有效结果；保存 worker/session/execution、profile、后端和日志。
4. GPU 正在计算时关闭共享：原进程退出/显存释放，旧结果不能提交，未完成图片在 CPU 接续，成功结果保持。
5. GPU 执行中取消 Job：不迁移；之后新 Job 能重新准备并执行。
6. GPU 进程崩溃、中心/网络中断、无效 device-id、缺失 CUDA/cuDNN、显存不足：不得静默 CPU 成功；状态、恢复和三次尝试上限正确。
7. 同一数据集冷/热 CPU 与 GPU 对照，记录正确吞吐、延迟和内存/显存。GPU 接通并不保证所有图片更快。

依赖依据：[ORT CUDA 兼容表](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)、[ORT profiling](https://onnxruntime.ai/docs/performance/tune-performance/profiling-tools.html)。
