# Edge Fabric 前端

P3 的前端位于 `web/`，使用 `zyc` 的 0.2.0 接口契约。`mock/fixtures.json` 引用提交 `cf6214a` 的 `D题参赛执行包/contracts/examples.json`，演示文件和场景由本地进程提供。

## 离线演示

需要 Node.js 22 或以上版本。Windows PowerShell 可从仓库根目录运行以下命令。

```powershell
cd web
npm.cmd run demo
```

打开终端显示的地址，默认是 `http://127.0.0.1:5173/`。选择图片 OCR 或短录音转写，点「使用演示文件」「提交任务」，在任务记录查看预览和下载 ZIP。使用 `Ctrl+C` 停止进程。

- 「模拟场景」可切换无资源、模型准备中、部分失败、P1 断线、P2 断线和类型未配置。部分失败会给下一批提交的第二个文件返回失败，点击「使用演示文件」即可选入两个不同文件。
- 可在执行中取消任务，在设备页退出或恢复接单。刷新可用 Job ID 恢复查询，演示任务和文件随进程退出清除。
- 演示返回固定的合成 OCR/ASR 文本。WAV 样例是合成音调。浏览器会给页面和下载的 ZIP 标上 MOCK，识别内容和设备读数用于验证界面。

## 连接真实服务

复制 `.env.example` 为 `web/.env`。将 `RESOURCE_BASE_URL` 和 `TASK_BASE_URL` 填为前端进程可访问的服务地址，每项只填协议、主机和端口。`DEMO_TOKEN` 可由环境变量或页面的「连接设置」提供。页面输入的令牌仅保存在内存中，刷新后可重新输入。环境文件已加入忽略规则。

```powershell
cd web
npm.cmd start
```

默认启动为真实模式，开发代理将 `/api/resource/v1` 转发到 P1，将 `/api/task/v1` 转发到 P2。两个服务各自报告连接状态。`WEB_HOST` 和 `WEB_PORT` 可调整监听地址和端口。生产可使用同源反向代理，保留页面的这两个路径和返回模式的 `/config` 路由（正式值为 `{"mock":false}`）。

联调时先确认两个 `healthz` 都返回 `contract_version: 0.2.0`，再用已配置的 profile 和一个新文件走完上传、创建、查询、预览和 ZIP 下载。真实设备执行、跨机贡献和识别准确度由联调和验收测试记录。
