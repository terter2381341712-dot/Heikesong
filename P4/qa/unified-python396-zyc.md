# zyc：全部 Python 环境统一为 3.9.6

2026-11-15，按用户要求将启动器、P1 CPU、P2、可选 CUDA 的 Python 统一固定为完整版本 3.9.6。不同组件仍使用各自的应用专用目录，CPU/GPU ORT 不混装；Web 使用 Node.js 22，不是 Python 组件。旧的 3.11.9 专用环境不再通过准备核验，将进入安装器修复流程。系统 Python、现有开发环境及业务数据未替换。

修改范围：两种平台的 bootstrap、managed_runtime 安装/核验、控制台开发覆盖版本检查、P2 项目版本与 uv.lock、CUDA profile 的 Python 字段及使用文档。默认 CPU profile、模型、模型 SHA、RapidOCR3.10.0、CPU/GPU ORT1.19.2、Pillow11.3.0 保持不变。CUDA profile 字节改变，因此两端更新后需重新配对，新的任务使用最新 profile digest。

实际运行暴露并修复了两项兼容差异：P2 结果下载/对账及预取使用 asyncio.TimeoutError，兼容 Python3.9 与3.11的异常类型差别；P1 的执行阶段耗时使用高分辨率 perf_counter，避免 Windows/Python3.9 的短时解码计为0。P2 进程测试显式设置模块根目录，支持嵌入式解释器忽略 cwd/PYTHONPATH。

## 验证

| 检查 | 结果 | 证据 |
|---|---|---|
| Python3.9.6，P1 完整测试（在 P1 目录执行） | 132 PASS | unified396-zyc-p1.xml |
| 按新 P2 uv.lock 安装的独立 Python3.9.6 环境，完整任务测试 | 153 PASS | unified396-zyc-p2-locked.xml |
| Python3.9.6，P4 完整工具回归 | 117 PASS / 1 SKIP | unified396-zyc-p4.xml |
| 控制台 JavaScript | 3 PASS | node --test P4/tests/control_ui.test.cjs |
| P2 uv lock --check --offline | PASS | uv0.13.8，34个包的锁文件 |
| P2 独立锁定环境 uv pip check | PASS | 33个已安装包无依赖冲突 |
| Windows/Python3.9.6 GPU 依赖解析 | PASS | 本机忽略目录 var/gpu-python396-resolved.txt |
| 修改的 P2/P4 文件 Ruff、git diff --check | PASS | 无新格式或静态检查错误 |

P4 跳过项为需要 POSIX 的真实 venv 路径测试。unified396-zyc-p2.xml 是首次检查的失败记录：当时异步超时兼容尚未修复、嵌入式模块入口未补齐；最终结果以带 locked 的报告为准。Windows GPU 安装包支持 Python3.9，可见 [ONNX Runtime GPU1.19.2 的 PyPI 发布文件](https://pypi.org/project/onnxruntime-gpu/1.19.2/)。依赖解析不代表 GPU 驱动、DLL、CUDA 推理已经通过。

## 尚未验证的边界

本轮没有声称从空环境通过 Windows/macOS/Linux 一键安装，也没有执行双机协作或真实 GPU 推理。此前 micromamba 报 Python/pip 不存在的异常不能仅靠替换版本号认定解决；此前本机下载的索引确实含这两个包，工具调用 micromamba --version 仍有异常退出，原因尚待进一步诊断。本轮代码统一版本与测试通过，不等于自动下载故障已修复。

更新后两台均退出旧启动器再按 P4/start.cmd 或 P4/start.command 重新准备环境；不要在任务执行期间替换环境。CPU/GPU 模型依赖仍需准备校验，失败不跳过或静默回退。
