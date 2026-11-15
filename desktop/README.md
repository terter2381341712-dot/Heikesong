# Edge Fabric 桌面外壳

一个 Electron 窗口替代原来的终端：双击打开，准备好环境后直接显示工作台，关窗口时停掉所有本机服务。

它**不改变任何启动逻辑**——仍然调用 `P4/scripts/bootstrap.sh`（macOS/Linux）或
`P4/scripts/bootstrap.ps1`（Windows），再由它们准备环境并拉起 `control.py`。
外壳只做四件事：显示进度、把 `control.py` 当子进程管、把工作台载入窗口、退出时清理。

中心的启动方式不变，仍然在工作台里点 **启动中心**。

## 开发运行

```sh
cd desktop
npm install
npm start
```

首次 `npm install` 需要下载 Electron 二进制（约 100 MB）。国内网络可先设镜像：

```sh
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ npm install
```

## 打包

```sh
npm run dist:mac    # 生成 .dmg
npm run dist:win    # 生成 .exe 安装包
npm run dist        # 只生成未打包目录，便于快速验证
```

打包时 `extraResources` 会把 `P1/ P2/ P4/ web/` 复制进应用包（排除 `__pycache__`、
`.pytest_cache`、`var/`），运行时从 `process.resourcesPath/app` 定位仓库根目录。
运行环境仍然准备在用户目录，不打进安装包。

## 文件

| 文件 | 作用 |
|---|---|
| `main.js` | 主进程：定位仓库根目录、启动准备脚本、轮询工作台、退出时停服务 |
| `preload.js` | 只向页面暴露日志与三个恢复动作 |
| `renderer/status.html` | 准备阶段的进度窗口 |
| `renderer/status.css` | 与工作台一致的配色（绿色主色、浅色纸面） |
| `renderer/status.js` | 把主进程事件渲染成进度条、日志与错误态 |
| `renderer/logo.svg` | 应用图标图形 |
| `preview-server.mjs` | 仅用于在浏览器里预览进度页，不参与应用运行 |

## 与工作台的边界

工作台 `web/` 自己的 CSP 是 `frame-ancestors 'self'`，所以外壳不是用 `<iframe>`
嵌页面，而是在工作台健康检查通过后把**整个窗口导航**到 `http://127.0.0.1:5173/`。
这样工作台的同源检查、令牌校验和 CSP 都保持原样，没有被放宽。
