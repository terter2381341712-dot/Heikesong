> 第二阶段更新：已实现 CPU 多槽、线程预算和 P2 剩余容量派发；GPU 保持单卡单槽。公开契约升级为 0.3.0，P1/P2/节点须同步升级。当前启动方式与行为以 [多槽使用说明](docs/CPU多槽与线程预算.md) 为准，以下历史阶段记录保留供追溯。

> 2026-11-14 最新更新：已接入 CUDA 执行路径并实现 CPU/CUDA 模型常驻；本机 CPU 测试通过，Windows GPU 实机待验。启动方式、兼容性与验证范围见 [本阶段报告](../qa/p1-p2/GPU与常驻模型实施报告.md)。下文早期阶段描述保留供追溯。

> 2026-11-14 后续实现更新：计算终止、共享关闭与 P2 迁移已实现，新增交互式 CLI；当前行为、接口和测试范围以 [实施报告](../qa/p1-p2/共享开关实施报告.md) 为准。以下此前未实现/原样导入描述保留为阶段背景。

# P1 资源服务与节点

本目录引入指定仓库 [Heikesong](https://github.com/terter2381341712-dot/Heikesong) 的 `wzq@7368ad01c9b1e8eefa546896a208ddac694eb33f`，提交标题为 `fix(p1): 按统一计划核验模型与节点会话`。来源及 41 个原始文件的 SHA-256 见 [SOURCE.json](SOURCE.json)。原始校验值保留用于追溯；本地实现变更与新增文件分别记录在 local_modifications/local_additions，分支中的旧根目录 P2 未复制。

本地 P2 仍在 [P2/](../P2/README.md)。本次基础连接采用 [统一计划 1.0](../P1-P2统一修改与联调计划.md) 和 OpenAPI 0.2.0；[真实联调报告](../qa/p1-p2/实际联调报告.md) 记录已验证范围。用户后续确认的“关闭共享立即中止并迁移剩余任务”需求见 [补充计划](../P1-P2设备共享开关与任务迁移补充计划.md)，尚未实现。

## 独立环境

当前 OCR 锁文件要求 **Python 3.9.6**、RapidOCR 3.10.0、ONNX Runtime 1.19.2、Pillow 11.3.0。不能直接用 P2 的 Python 3.11 环境运行节点，也不能更改锁文件来掩盖环境不符。

以下从项目根目录执行。本机 `/usr/bin/python3` 已核实为 3.9.6；其他电脑请将该路径换为实际的 Python 3.9.6：

```sh
/usr/bin/python3 --version
uv venv --python /usr/bin/python3 P1/.venv
uv pip install --python P1/.venv/bin/python -r P1/requirements.txt
```

P2 按其 README 在 `P2/` 内执行 `uv sync --frozen`。

## 本机连接与复验

从项目根目录执行：

```sh
# 临时真实服务 + 节点；上传图片、建 Job、校验 OCR、暂停恢复与重启；结束即清理进程
P2/.venv/bin/python qa/p1-p2/run_local.py check

# 明确启动本机资源共享：P1 8001、P2 8002、一个真实 OCR 节点；Ctrl+C 停止
P2/.venv/bin/python qa/p1-p2/run_local.py serve
```

工具分别使用两套解释器，显式注入地址、独立数据目录及角色令牌。`serve` 数据默认在 `qa/p1-p2/var/live/`；`connection.json` 仅保存在本机，勿提交或复制到报告。安装依赖不会启动共享，执行 `serve` 才会启动并注册节点。这是命令行联调入口，尚无安装器或桌面共享开关；Ctrl+C 停服务也不代表实现了剩余任务迁移。

原分支的 [启动与验收说明](docs/P1-启动与验收.md) 保留原文；其中“仓库根目录”在当前工作区对应 `P1/`。独立运行时先 `cd P1`，将其示例的 `var/venv-p1/bin/python` 换为 `.venv/bin/python`，按原说明导出环境变量；程序不会自动加载 `.env`。

## 跨电脑连接

当前本机工具只绑定 `127.0.0.1`。跨机时，按原启动说明将资源服务和 P2 部署在协调电脑，绑定可达网卡；节点和 P2 的 `RESOURCE_BASE_URL` 指向同一个中心资源服务。每台设备运行自己的 P1 节点程序，使用独立的 `NODE_AGENT_DATA_DIR` 和同版 profile。

节点主动注册、发送心跳并领取中心已分配的工作；P2 通过公开资源 API 调度，不直接连接设备的 IP/端口。两台物理电脑协同及 Windows 运行仍待实际设备验收。

## P1 回归

```sh
cd P1
.venv/bin/python -m pytest -q tests/resource
```

本次原提交的 51 项测试全部通过，未改动 P1 实现或自检脚本。当前联合工具的结果不能代替计划 L01–L19 的全部验收。
