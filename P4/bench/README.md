# 质量与性能证据

工作台设备详情的 CPU/GPU 吞吐量、PDF 聚合效率公式及报告导入方法见 [设备性能指标](设备性能指标.md)。

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
.venv/Scripts/python.exe -m node_agent.benchmark --manifest ../P4/var/run-01/inputs.jsonl --profile profiles/ocr.default.v1.lock.json --backend cpu --threads 2 --output-dir ../P4/var/baseline-PC-A --run-id baseline-PC-A > ../P4/var/baseline-PC-A.jsonl
```

使用 P1 的入口，不另写推理。JSONL 含逐文件 BaselineRecord 和 BaselineSummary，全部按共享 schema 校验并与相同人工参考对照。注意 PowerShell 5 的 `>` 默认 UTF-16，分析时明确相应编码或用 Python subprocess 以二进制文件捕获 stdout；转换编码不能改变原始证据。第一次下载、加载、自测、预热分开报告。

5. 分别测 P1 单节点服务、P2 单节点完整服务、固定同类型多节点。P1 单节点基准需 P1 负责人提供按公开接口接纳/结束记录的实验入口；本轮 verify 是 P2 API 路径，不冒充 P1 单节点基准。单节点实验先关闭其他节点共享；固定多节点实验再统一开启。verify 不固定实际参与集合，正式实验须人工核对资源快照与 executions 中所有计算者。
6. 每路径足量不同输入，至少3轮分别报告及中位数；不可把重复轮叠成有效工作。采样目标3秒；verify 的采样是同步轮询，会受网络延迟影响，实际覆盖与间隔按 telemetry.jsonl 检查，间隔不足时不能签收正式采样。动态加减节点/掉线/迁移单列，不纳入固定设备效率。

正式 P2 窗口使用服务端 Job 持久接受至全部逻辑 Task 终态；P1 窗口为接受至执行全部终态，本地窗口为读输入、推理、后处理、写规范 JSON。verify 的 observed_job_seconds 从提交请求前至终态观测，仅供联调，不直接替代正式服务窗口。含上传总耗时单列，ZIP下载不计主窗口。只在同一电脑用 monotonic 计算客户端耗时，不跨电脑墙钟相减。

设每设备本地完整执行器**经人工参考质量核验**正确吞吐为 P_i，多机服务正确吞吐为 P_pool：聚合效率 = P_pool / sum(P_i)，最强单机提升 = P_pool / max(P_i)。中心 A 若参与计算也在分母。失败、取消、重复不加分子，全部耗时保留；没有本地基线或质量阈值时，效率填 null/N/A。OCR用正确图片/s；ASR用正确音频秒/墙钟秒，两者不相加。

正式发布还需 config/输入类别与许可、冻结窗口、逐执行和采样 CSV、summary.csv、事件 JSONL、quality.md、report.md；verify 尚未替代这份人工审定报告。当前正式 B01–B06 全部 NOT_RUN。
