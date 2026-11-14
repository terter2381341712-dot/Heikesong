# Edge Fabric 前端

P3 的前端位于 `web/`，使用 `zyc` 的 0.2.0 接口契约。`mock/fixtures.json` 引用提交 `cf6214a` 的 `D题参赛执行包/contracts/examples.json`，演示文件和场景由本地进程提供。

## 离线演示

需要 Node.js 22 或以上版本。Windows PowerShell 可从仓库根目录运行以下命令。

```powershell
cd web
npm.cmd run demo
```

打开终端显示的地址，默认是 `http://127.0.0.1:5173/`。在首页图谱点「上传图片」，选择图片 OCR 或短录音转写，再点「使用演示文件」「提交任务」。任务创建后会自动打开「任务进度」，通过「查看识别结果」预览识别文本并下载 ZIP。使用 `Ctrl+C` 停止进程。

- 首页图谱的四个入口在当前页打开窗口，点击关闭按钮或按 `Esc` 即可返回图谱。上传窗口关闭后，本次会话中已选的文件可继续使用。
- 首页粒子视频、桌面四面体和流动连线在窗口打开或浏览器标签页隐藏时自动暂停，也可通过「设置」里的「播放首页动效」关闭。系统开启减少动态效果时使用静态画面。
- 「模拟场景」可切换无资源、模型准备中、部分失败、P1 断线、P2 断线和类型未配置。部分失败会给下一批提交的第二个文件返回失败，点击「使用演示文件」即可选入两个不同文件。
- 可在「任务进度」窗口取消执行中的任务，在「设备资源」窗口退出或恢复接单。刷新可用 Job ID 恢复查询，演示任务和文件随进程退出清除。
- 演示返回固定的合成 OCR/ASR 文本。WAV 样例是合成音调。浏览器会给页面和下载的 ZIP 标上 MOCK，识别内容和设备读数用于验证界面。

## 启动 Hub 工作台

先启动 P1 资源服务和 P2 任务服务。具备 P1、P2 环境后，也可在仓库根目录运行本机联调入口。以下为 Windows PowerShell 命令。

```powershell
.\P2\.venv\Scripts\python.exe qa/p1-p2/run_local.py serve
```

该命令会启动一个本机 OCR 节点。浏览器使用的 `DEMO_TOKEN` 位于终端输出的数据目录下的 `connection.json` 中。另开终端，将 `web/.env.example` 复制为 `web/.env`，填写 Web 进程可访问的 P1、P2 地址，每项只填协议、主机和端口。示例对应同机部署的 `8001` 和 `8002`。在 Hub 所在机器运行以下命令。

```powershell
cd web
npm.cmd start
```

默认是真实模式，使用 `/api/resource/v1` 访问 P1，使用 `/api/task/v1` 访问 P2。Hub 同时把 Node 使用的 `/api/resource/internal/v1/nodes/…` 转发到 P1，由 P1 校验 Node 的注册和会话凭证。P2 使用自己的 `RESOURCE_BASE_URL` 连接 P1 执行调度。

同网设备接入时，在 `web/.env` 中设置 `WEB_HOST=0.0.0.0`。将 `http://<Hub 局域网 IP>:5173` 提供给用户和 Node。浏览器打开该地址，在「设置」输入 `DEMO_TOKEN`。令牌只保存在页面内存中，刷新后重新输入。受信任的演示网络也可给 Web 进程配置 `DEMO_TOKEN`，由它为浏览器转发用户请求。

在贡献算力的设备上按 P1 文档配置 `JOIN_TOKEN` 和本机 `NODE_AGENT_DATA_DIR`，然后从 `P1/` 目录用该设备的 P1 Python 环境启动 Node。以下为 Windows 示例，将地址替换为实际 Hub 地址。

```powershell
.\.venv\Scripts\python.exe -m node_agent --server 'http://<Hub局域网IP>:5173' --interactive
```

Node 启动后输入 `on` 加入资源池，输入 `off` 停止共享。浏览器可在同一台设备上独立提交和查询任务。「设备资源」中的「暂停接新任务」用于排空节点，已经接到的工作会继续。开启或关闭本机共享在 Node 程序中完成。

当前 P2 目录只提供图片 OCR，文件格式、大小上限和语言选项以 P2 目录为准。联调时用新 JPG 或 PNG 提交，查看逐图进度和分派记录，并核对识别结果、原图及 ZIP 下载。
