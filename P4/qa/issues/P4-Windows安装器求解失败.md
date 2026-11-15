# Windows 启动器报 Python/pip 不存在

## 现象与定位

Windows 在准备 bootstrap 环境时，micromamba 报 pip 和指定 Python 不存在。下载的 win-64/noarch 索引实际含这些包；改为 Python3.9.6 后同样失败。工具会话执行这个固定哈希的 micromamba 时，连 --version 也异常退出，尚不足以断言其内部具体根因是缓存、路径还是系统环境。

本次从 Windows 默认 CPU/中心的关键启动路径移除了对 Conda 求解的依赖，保留 Python3.9.6、版本校验和自动依赖准备。

## 修改

- bootstrap.ps1 使用 SHA256 固定的 Python.org Python3.9.6 便携包，仅初始化应用私有目录，不借用或修改系统解释器。
- windows_runtime.py 为 CPU/P2 准备相同版本 Python 和固定 pip25.3，为工作台准备 Node22.14.0；官方包清单为 runtime/portable-windows.tsv。Node SHA 同时与官方 SHASUMS256 核对。
- 嵌入式 Python 不使用 pip 临时 PYTHONPATH 构建环境，因此 Windows 私有 P1/P2 先安装固定 setuptools80.9.0 和 wheel0.45.1，再以 --no-build-isolation 安装依赖；否则 antlr4 的构建会报 setuptools.build_meta 不存在。
- 基础环境先在临时目录验证，旧环境保留为 backup。Windows 阻止移动刚验证的可执行目录时，有限重试后仅复制到不存在的目标并再次验证，不覆盖运行中的目录或删除源。业务身份、配对配置、原图和任务数据不参与移动。
- macOS/Linux 和可选 CUDA 专用库保持原安装路径；本次不宣称修复所有 CUDA 安装问题。Python版本、模型和 OCR 冻结依赖保持不变。

## 实测结果

2026-11-15，本 Windows x64 工作区包含中文路径。执行新的 PowerShell 启动器 --prepare-only，实际建立 bootstrap/P1/P2/web 环境，完成 P1/P2 的 pip check 与模块导入，再次执行复用全部通过的环境。

| 验证 | 结果 | 证据 |
|---|---|---|
| 私有 bootstrap/P1/P2 Python | 全部3.9.6 | 本地 runtime目录及真实按钮报告 |
| 私有 Node | 22.14.0 | 固定ZIP、版本检查 |
| P4全量工具 | 124 PASS / 1 SKIP | ../portable-windows-pytest.xml |
| 新私有 P1 环境真实模型测试 | 7 PASS | ../portable-real-ocr.xml |
| 中心/节点/工作台、共享开关、真实OCR、结果查询和ZIP | PASS | ../portable-control-real-smoke.json |
| 既有控制台JS | 3 PASS | node --test P4/tests/control_ui.test.cjs |
| 修改文件Ruff、PowerShell语法解析 | PASS | 本机检查 |

P4跳过项为POSIX专用测试。真实服务检查使用独立控制/工作台端口，结束后已停止测试服务，确认8001/8002及测试端口无监听。

测试使用已从官方源下载并校验的基础归档；本工具会话内 PowerShell 对外 HTTPS 请求仍出现接收错误，因此不宣称已验证纯净、无预装软件机器的首次HTTPS下载。实际依赖下载和安装通过新私有 Python 完成。网络若阻断官方站点，仍需先解决下载可达性，代码不关闭TLS证书校验。

## 使用

退出旧启动窗口后，仍执行 P4/start.cmd。启动器会检查并复用已经准备好的环境；无须手工清理原 micromamba 缓存。完整安装日志在 P4/var/runtime/setup.log。环境目录、归档和下载缓存仍被Git忽略。
