> zyc 集成采用契约 0.4.0；新增计算配置、两人双机验收和吞吐量步骤见 [集成验收](集成验收.md)。历史 qa 报告不代表本轮通过。

# P4 · 联合启动、验收和演示

当前启动器、P1、P2、可选 CUDA 的 Python 均固定为 **3.12.15**。下方旧复验记录使用过 3.9.6/3.11，保留为历史；当前依赖以 P2/pyproject.toml、uv.lock、P1/requirements.lock.txt 与各组件 profile 为准。工作台仍使用 Node.js 22。

日常操作走 [一键连接](一键连接.md)。中心控制台启动 P1、P2 和 Edge Fabric 工作台，可同时启动本机计算。另一台电脑只运行 P1 节点即可。下文是命令行分角色与正式验收的流程。

[任务卡](../D题参赛执行包/tasks/P4-验收压测与演示.md)和[验收矩阵](../D题参赛执行包/03-验收与压测.md)定义交付范围。[qa/](qa/) 保留 lyj 分支的阶段证据与问题记录。涉及旧分支运行环境、旧网卡和历史状态时，以当前启动说明、各组件 README 及本轮复验结果为准。

## 主线集成复验

- Windows 单机以 P1 Python 3.9.6、P2 Python 3.11.9 启动（历史记录，当前统一为 3.12.15）。契约静态校验通过，P1 55 项、P2 101 项、P4 64 项 Python 测试通过。P4 跳过 1 项需要 POSIX 环境的测试，控制台 JavaScript 测试 3 项通过。
- 控制台的两图测试通过真实 OCR 和 ZIP 校验。浏览器工作台完成令牌鉴权、上传 PNG、查看任务进度、预览 OCR、下载与输入字节一致的原图及结果 ZIP，并核对了真实节点和英文界面。
- 在一台 Windows 电脑上用两个独立节点目录验证了配对和共享开关。自动发现进行了 UDP 回归测试。两台物理设备贡献同一 Job、跨系统实机和正式压测仍按验收矩阵登记。

## 示例：两台电脑怎么连

```text
电脑 A（你的电脑，例 192.168.1.10）
  中心 P1 资源服务 :8001 ← 本机 P1 节点
           ↑              ↑
           │              P2 调度后节点主动领取
           ├────────── 电脑 B 的 P1 节点（无须监听端口）
           ↑
  P2 任务服务 :8002 ← 验收客户端 / Edge Fabric 工作台
```

**整个集群只用一个中心资源服务。** 另一台电脑的“运行 P1”是运行 `node_agent`，不另开资源服务。你这台同时运行 P1 资源服务、P2 和一个 P1 节点，就能与另一台共同计算。同一电脑多个进程只算一个物理设备。

| 电脑角色 | 命令角色 | 必需环境 | 配置文件 |
|---|---|---|---|
| A：中心 P1、P2、本机计算 | center --with-node | Python 3.12.15 | center.json |
| B：只提供计算 | node | P1 Python 3.12.15 | node.json，仅 JOIN |
| C：只运行 P2（可选） | task | Python 3.12.15 | task.json，DEMO/TASK |
| 验收电脑 | verify.py | Python 3.12.15 | client.json，DEMO/TASK |

若 P2 放电脑 C，A 改为 `--role resource`（另外启动本机 node），C 使用 `--role task`。所有配置中的 resource_url 仍指向 A。只保留一个连接此中心的 P2，避免多个独立调度器竞争；P2 的健康检查只证明它存活，不证明 P1 可达。

## 1. 自动准备环境

普通使用请在每台电脑运行一键启动器，Mac 和 Windows 都会自动准备专用环境，无需安装或切换系统 Python。见 [一键连接](一键连接.md) 与 [自动环境准备](自动环境准备.md)。以下手工命令仅用于维护开发环境，不是产品启动前提。

### 开发维护用手工环境

当前真实 OCR 锁定 Python **3.12.15**、RapidOCR **3.10.0**、ONNX Runtime **1.19.2**、Pillow **11.3.0**；Python 3.12 的其他补丁版本也会被拒绝。先安装这些解释器，不能改锁文件绕过核验。Windows PowerShell 示例：

```powershell
# py 启动器未登记解释器时，用真实 Python 3.12.15 路径替换 py -3.12
py -3.12 -c "import platform; assert platform.python_version() == '3.12.15'"
py -3.9 -m venv P1/.venv
P1/.venv/Scripts/python.exe -m pip install -r P1/requirements.lock.txt

# 中心或独立 P2 电脑安装；推荐沿用仓库 uv.lock
uv sync --project P2 --frozen --python 3.12.15

# 验收电脑安装；启动工具 cluster.py 本身只依赖标准库
py -3.9 -m venv P4/.venv
P4/.venv/Scripts/python.exe -m pip install -r P4/requirements.txt
```

无 uv 时先在自己的工具环境安装 uv，再按 P2 README 使用 uv.lock 创建环境。B 只需完整项目代码和 P1 环境；不必安装 P2 或 P4 依赖，直接使用 P1 解释器运行 cluster.py。

macOS/Linux 使用相同参数，将 `Scripts/python.exe` 换成 `bin/python`，创建 P1 环境时使用实际 Python 3.12.15 路径。模型锁来自当前仓库的原始字节，不要在另一台电脑上转换换行。

## 2. 网络准备

A 执行 `ipconfig` 查出连接同一局域网的 IPv4，把下文 **192.168.1.10 换成 A 的实际 IP**。B 不可填写 127.0.0.1，连接地址也不可填写 0.0.0.0。同一 Wi-Fi 不一定互通，访客网络/AP 隔离可能阻止访问。

A 的防火墙允许 Python 对受信任局域网监听 TCP 8001、8002；B 的节点仅主动出站，无须开放入站端口。工具不改防火墙。如果 B 只提供计算，连通 8001 即可；在 B 上执行完整验收还需要连通 8002。

```powershell
# B 上执行，A 启动之后两项应 TcpTestSucceeded=True
Test-NetConnection 192.168.1.10 -Port 8001
Test-NetConnection 192.168.1.10 -Port 8002
```

## 3. A 生成配置并启动（先 A 后 B）

```powershell
P4/.venv/Scripts/python.exe P4/scripts/cluster.py init --resource-url http://192.168.1.10:8001 --task-url http://192.168.1.10:8002
P4/.venv/Scripts/python.exe P4/scripts/cluster.py run --role center --with-node --node-name PC-A --config P4/var/config/center.json --data-dir P4/var/center
```

第一次 init 生成三个不同随机令牌，重启复用配置，不再 init。默认监听 0.0.0.0，端口 8001/8002，P1/P2 使用各自 `.venv`；可用 `--p1-python`、`--p2-python` 指定实际解释器。变更监听端口时，同时修改 URL 配置。

把 **node.json 单独复制到 B 的 P4/var/config/node.json**。它仅包含 JOIN_TOKEN；不要复制整个 A 的 var、数据库、安装身份或 center.json。需要在 B 验收时，再单独复制 client.json。所有配置含凭证，放在已忽略的 var 内，不放入报告；Windows 私密性由文件夹 ACL 控制，限制给本人和必要运维人员。

## 4. B 连接 A

```powershell
P1/.venv/Scripts/python.exe P4/scripts/cluster.py run --role node --node-name PC-B --config P4/var/config/node.json --data-dir P4/var/remote
```

节点会自动注册、心跳，初始 NOT_PREPARED 很正常；提交 Job 后由 P2 自动准备并执行。日志在各自 data-dir；节点身份保存在各自 `nodes/PC-*`，不要跨电脑复制它，否则可能被识别成同一个节点。每个节点名和数据目录保持稳定，重启沿用原配置与原目录。

可加 `--interactive`：初始不共享，在此终端输入 `on` 开启，`off` 关闭，P1/P2 按已实现逻辑停止计算并迁移未完成工作；用另一个终端观察 Job。只有关闭已被服务确认才算停止，不能把按键本身作为验收证据。非交互模式启动后立即注册共享。

Ctrl+C 停本次启动的进程，资源与任务数据保留。Windows 停进程时清理本次子进程树；这属于停止进程，优雅关闭共享演示应先用 `off`。工具不会停止已有服务。端口被占用或子进程退出会报错，查看日志；环境变量由工具显式注入，不依赖 .env 自动加载。局域网子进程禁用继承代理，避免请求误走外部代理。

## 5. 接口与真实 OCR 验收

```powershell
# 契约字节一致、schema 与 55 个样例；仅 STATIC
P4/.venv/Scripts/python.exe P4/scripts/verify.py --static --out P4/var/static-01

# 在 B 执行可先验证跨机地址、health、角色、目录、资源；需 B 安装 P4 环境
P4/.venv/Scripts/python.exe P4/scripts/verify.py --config P4/var/config/client.json --health-only --out P4/var/health-01

# 在验收电脑准备自己拥有授权的不同 JPG/PNG 放到 P4/var/inputs，至少 20 张有助于两节点都分到工作
P4/.venv/Scripts/python.exe P4/scripts/verify.py --config P4/var/config/client.json --inputs P4/var/inputs --out P4/var/run-01 --timeout 600
```

每次 out 必须是新目录，避免覆盖证据。失败返回 1，中断返回 130，PASS 返回 0。等待超时保留原 Job，不自动取消或另开执行。若创建响应丢失，从 `job-request.json` 取出原 client_request_id、原载荷，用原请求重放 POST jobs 对账，不改 UUID；不能把重新运行整轮当成同 ID 恢复测试。

脚本只提交上传与 Job，由 P2 发 preparation/execution；不自行调度。检查 SHA/长度/字节、上传重放、Job 同载荷同 ID/改载荷409、Task 顺序、执行输入/profile/状态、P1 与 P2 结果一致、结果 SHA 和 ZIP；保存失败状态、重复尝试的全部 dispatch。它验证 API 链路，浏览器验收 I01/U01 可再用工作台真实提交并下载结果。

看 `report.json` 中 initial_resources/final_resources 的 worker_id。按 [设备映射模板](config/devices.example.json) 填入两个真实物理设备并由操作人将 operator_attested 改 true，保存在 `P4/var/devices.json`：

```powershell
P4/.venv/Scripts/python.exe P4/scripts/verify.py --config P4/var/config/client.json --inputs P4/var/inputs --out P4/var/two-pc-01 --min-workers 2 --devices P4/var/devices.json
```

只有同 Job 两个节点各贡献有效成功结果，且人工确认来自两个独立设备，才记 `PASS_WITH_OPERATOR_ATTESTATION`。节点多但一台没有贡献会 FAIL；可增加不同输入，检查另一台是否 ONLINE/ACTIVE、有空槽以及锁文件核验状态。工具不会靠修改调度器强行均分。

完整记录在 [验收清单](qa/acceptance-results.json)，初始化全部 NOT_RUN。按具体用例的全部通过条件人工登记 PASS/FAIL、两端版本、设备、时间、命令、原始证据；脚本的子项 PASS 不自动签收整条 R/T/I/B 用例。缺陷记录模板和当前执行范围见 [验收报告](qa/验收报告.md)。

## 6. 质量、压测、模拟与演示

- [质量与性能步骤](bench/README.md)：独立参考、CER、P1 离线基线、单节点和固定多节点口径，不能用成功数冒充正确吞吐。
- [演示流程](demo/README.md)：新输入、两机贡献、下载、共享变化、恢复与干净重启。
- 现有 P1 模拟入口为 `P2/tests/tasks/mock_p1.py`，显式设置 `P2_ENABLE_MOCK_P1=1`，操作见 [P2 README](../P2/README.md)。配合真实 P2 可供客户端协议开发，预置文件 ID，未实现真实上传。故障/等待/取消行为由 P2 tests/tasks 覆盖；工作台有独立的 MOCK 模式，见 [工作台 README](../web/README.md)。

当前真实环境开放 OCR，ASR 未配置。两台独立物理电脑对同一 Job 的贡献、正式质量和压测须在现场验证。

工具回归：

```powershell
P4/.venv/Scripts/python.exe -m pytest -q P4/tests --basetemp=P4/var/pytest-tools --junitxml=P4/var/tools-pytest.xml
node --test P4/tests/control_ui.test.cjs
```

验收控制台时先启动一个空闲控制台，然后执行下列命令。脚本会启动并停止自己的服务，校验工作台真实模式、两端健康、共享开关、真实 OCR 和 ZIP。

```powershell
P4/.venv/Scripts/python.exe P4/scripts/check_control.py --address 127.0.0.1 --out P4/var/control-smoke-01.json
```

如需同时检查局域网发现和配对，省略 `--address 127.0.0.1`，并确保当前控制台已绑定 UDP 37043。
