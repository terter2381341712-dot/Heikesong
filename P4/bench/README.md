# 质量与性能证据

按钮入口会将运行profile副本统一为LF，记录在 `P4/var/control/profiles/manifest.json`。用按钮创建的Job做离线基线时，应使用 **P4/var/control/profiles/ocr.default.v1.lock.json**，核对其SHA与Job的profile_digest相同。不要把手工CLI的原始CRLF锁文件与按钮运行副本的摘要混为同一版本证据；仅换行统一不改变JSON字段、Python或模型要求。

真实产物写入 `P4/var/<run-id>/`，提交报告前去除配置凭证。verify 生成 config.json（无令牌、代码 SHA）、inputs.jsonl、http.jsonl、job.json、results.json、results.zip、tasks.csv、executions.json/csv、telemetry.jsonl/csv、fault-events.jsonl、summary.csv、report.json。失败发生较早时后续产物缺失，保持 FAIL；telemetry.csv 保留每节点完整 raw_node_json，未知指标不填 0。事件按游标完整分页读取，保留全历史；正式分析按窗口与关联 ID 过滤。

1. 准备至少20张不同授权图片，包含困难、噪声、多语言和空文本；保留原始字节 SHA、来源许可、语言、尺寸、类别。另备短 WAV 及参考，但当前 ASR 不可执行，记 NOT_RUN。
2. 在运行前按 `P4/config/references.example.json` 人工冻结参考文本、归一化和 max_cer。示例阈值仅示例。真实参考保存在验收电脑，不传入 P1 模型目录或服务请求；将 frozen_before_run 设 true 是操作人对冻结时间的确认，工具本身不能证明时间。
3. 完成 verify 后在项目根目录执行：

```powershell
P4/.venv/Scripts/python.exe P4/scripts/quality.py --run P4/var/run-01 --references P4/var/references.json
```

生成 quality.json。归一化 NFC、统一空白/首尾去除，保留大小写和标点；CER 为编辑距离/参考字符数，空参考单列 exact match，失败不算正确图片。样本少于20时标 minimum 未满足。输出中的正确吞吐仅是客户端观察窗口，轮询和网络会带来误差，formal_benchmark 始终 NOT_RUN。

4. 每台设备复制相同输入字节和 `inputs.jsonl`，只把 local_path 改为该设备绝对路径，保持输入 ID/索引/SHA。同一 profile、后端、线程、槽位、批大小、设备集合先冻结。P1 本地完整执行器在每台运行（从 P1 目录，输出文件路径按实际改）：

```powershell
.venv/Scripts/python.exe -m node_agent.benchmark --inputs ../P4/var/inputs --profile profiles/ocr.default.v1.lock.json --backend cpu --output-dir ../P4/var/baseline-PC-A --run-id baseline-PC-A
```

使用 P1 的入口，不另写推理。输出目录中的 UTF-8 JSONL 含逐文件 BaselineRecord 和 BaselineSummary，全部按共享 schema 校验并与相同人工参考对照。保留同目录的后端记录和逐图输出。第一次下载、加载、自测、预热分开报告。已冻结 manifest 的轮次也可使用 `--manifest`。

5. 分别测 P1 单节点服务、P2 单节点完整服务、固定同类型多节点。P1 单节点基准需 P1 负责人提供按公开接口接纳/结束记录的实验入口；本轮 verify 是 P2 API 路径，不冒充 P1 单节点基准。单节点实验先关闭其他节点共享；固定多节点实验再统一开启。verify 不固定实际参与集合，正式实验须人工核对资源快照与 executions 中所有计算者。
6. 每路径足量不同输入，至少3轮分别报告及中位数；不可把重复轮叠成有效工作。采样目标3秒；verify 的采样是同步轮询，会受网络延迟影响，实际覆盖与间隔按 telemetry.jsonl 检查，间隔不足时不能签收正式采样。动态加减节点/掉线/迁移单列，不纳入固定设备效率。

正式 P2 窗口使用服务端 Job 持久接受至全部逻辑 Task 终态；P1 窗口为接受至执行全部终态，本地窗口为读输入、推理、后处理、写规范 JSON。verify 的 observed_job_seconds 从提交请求前至终态观测，仅供联调，不直接替代正式服务窗口。含上传总耗时单列，ZIP下载不计主窗口。只在同一电脑用 monotonic 计算客户端耗时，不跨电脑墙钟相减。

设每设备本地完整执行器**经人工参考质量核验**正确吞吐为 P_i，多机服务正确吞吐为 P_pool：聚合效率 = P_pool / sum(P_i)，最强单机提升 = P_pool / max(P_i)。中心 A 若参与计算也在分母。失败、取消、重复不加分子，全部耗时保留；没有本地基线或质量阈值时，效率填 null/N/A。OCR用正确图片/s；ASR用正确音频秒/墙钟秒，两者不相加。

正式发布还需 config/输入类别与许可、冻结窗口、逐执行和采样 CSV、summary.csv、事件 JSONL、quality.md、report.md；verify 尚未替代这份人工审定报告。当前正式 B01–B06 全部 NOT_RUN。

## 在设备详情发布 CPU / GPU 实测

工作台按已注册的 `worker_id` 显示结果。每一行对应一种 OCR 执行后端，GPU 对应 NVIDIA CUDA。单机数据先测，参与同一轮算力池的设备全部使用该后端。CPU 和 CUDA 各自形成一组单机基线和多机服务结果。

### 准备运行环境

- 使用原启动方式或 `NODE_OCR_BACKEND=cpu` 运行 CPU。中心和节点更新前先让旧任务结束，更新后重启并使用同一版模型锁。在控制台检查节点已注册，切换后端时沿用本机节点数据目录和设备名称。
- CUDA 节点需 NVIDIA 驱动、CUDA 12.x、cuDNN 9.x 和 Python 3.9.6。在专用虚拟环境执行 `python -m pip install -r P1/requirements-gpu.txt`。Windows 将 CUDA 与 cuDNN 的 DLL 目录加入 PATH，Linux 将共享库目录加入系统搜索路径。启动前设置 `P4_P1_PYTHON` 为该环境的 Python 路径，并设 `NODE_OCR_BACKEND=cuda`。普通 CPU 环境安装 `P1/requirements.txt`。三个 ONNX 模型均通过 CUDA 后端检查和校准后，节点才报告 CUDA 就绪。[ONNX Runtime CUDA 依赖说明](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
- 准备同一批至少 20 张不同的授权图片，预先冻结文本、SHA-256 与 CER 阈值。验收电脑另装 `P4/requirements.txt`。以下命令里的设备名、运行目录和 Python 路径按实际部署填写。

### 单机和多机测试

1. 在每台设备等现有任务完成后测单机。设置 `NODE_AGENT_DATA_DIR` 为本节点的身份数据目录（包含 `install-*.json`）。控制台默认以 `P4/var/control/data/nodes/<设备名>` 保存本机身份，实际路径可从启动日志核对。从 P1 目录用对应后端的 Python 执行。切换后端时换输出目录、运行 ID 和 `--backend`。

   ```powershell
   python -m node_agent.benchmark --inputs ../P4/var/inputs --profile ../P4/var/control/profiles/ocr.default.v1.lock.json --backend cpu --output-dir ../P4/var/cpu-PC-A --run-id cpu-PC-A
   ```

2. 在同一受控中心让该组设备全部开启接单，暂停其他设备。先提交预热任务，让这些设备都完成模型准备并确认已就绪，再停止其他批次。在控制台启动的中心本机执行下列命令。CLI 启动的中心改用受控的 `client.json` 和 `P1/profiles`。按 `P4/config/devices.example.json` 填写每个参与节点与独立物理设备的对应关系。

   ```powershell
   python P4/scripts/verify.py --config P4/var/control/center.json --inputs P4/var/inputs --out P4/var/pool-cpu --profiles-dir P4/var/control/profiles --min-workers 2 --devices P4/var/devices.json
   ```

3. 每台设备拷回单机输出目录，包括 `baseline-*.jsonl`、`baseline-*.runtime.json` 和 `results/`。在中心执行以下命令发布。控制台中心默认使用 `P4/var/control/data/resource`，CLI 中心使用启动时 `--data-dir` 下的 `resource` 子目录。发布 CUDA 时用 `--backend cuda` 和 CUDA 轮次的路径。只发布单机结果时省略 `--pool` 与 `--devices`。

   ```powershell
   python P4/scripts/performance.py --backend cpu --references P4/var/references.json --baseline P4/var/cpu-PC-A/baseline-cpu-PC-A.jsonl P4/var/cpu-PC-B/baseline-cpu-PC-B.jsonl --pool P4/var/pool-cpu --devices P4/var/devices.json --resource-dir P4/var/control/data/resource
   ```

每轮使用 `ocr-performance-cpu.json` 或 `ocr-performance-cuda.json` 存储。中心的工作台和已配对工作台读取同一份报告。文件包含吞吐、正确结果数、时间窗以及多机轮次的每节点贡献、配置、采样均值和最强单机加速比。正式记录保留足量负载下的各轮原始文件，按设备和后端量化瓶颈。设备详情展示最近一次发布且模型锁相符的结果。
