# 第四阶段：GPU 输入预取与解码流水线实施报告

2026-11-14，所有修改保留在 `zyc` 分支，未合并 `main`。本阶段已完成输入预取、CPU 解码与当前推理重叠、对应接口和本机验收；Windows NVIDIA 实机验收仍为 **NOT_RUN**。

## 实现

- P2 给启用预取的忙节点提示一张待调度输入；提示不占执行槽、不创建 execution、不扣 attempts、不阻止其他节点接单。仍按第三阶段实际剩余槽位及能力/耗时调度。
- 单槽 Agent 新增一个可终止的 CPU 输入进程，下载、SHA 校验、EXIF/RGB 解码和下一张推理重叠。GPU 模型进程仍常驻复用、单卡单槽。普通输入路径也去掉了重复完整解码。
- 预取容量固定 1，压缩文件不超过 10 MiB、图片不超过 2000 万像素；输入进程计入一个额外 CPU 线程预算。每请求独立临时目录，缓存消费后移入执行目录并删除；异常缓存/未完成预取回退普通路径。
- 提示具有会话、单调修订号与 15 秒 TTL。P2 先持久化再发送；同修订号同内容可刷新 TTL，旧修订号不能覆盖撤销。已接纳输入在提示切换期间保留，取消的等待输入仅停止自身预取。
- 当前执行取消、关闭共享、会话失效、心跳超时、退出和硬截止都先停止并 join 模型/解码进程，再发送原停止确认。普通取消不迁移，共享关闭保留 NODE_UNAVAILABLE 迁移语义，已成功结果保持。
- 公开契约为 **0.4.0**，三份 OpenAPI 及当前样例同步；P1/P2 自动扩展 SQLite 表，旧模型 profile 锁文件及模型 SHA 保持。启动选项、接口、计时定义及限制见 [使用说明](../../P1/docs/GPU输入预取与解码流水线.md)。

## 本机测试

| 验收 | 结果 | 证据 |
|---|---|---|
| P1 全部资源测试 | **116 passed**，新增 24 项 | [JUnit](prefetch-p1-pytest.xml) |
| P2 任务及联调工具测试 | **159 passed**，新增 13 项 | [JUnit](prefetch-p2-pytest.xml) |
| 真实输入预取、解码重叠、取消与迁移 | **PASS** | [完整记录](input-prefetch-real-result.json) |
| 原 CPU 双槽并发、单槽取消隔离、共享关闭迁移 | **PASS** | [回归记录](input-prefetch-multislot-result.json) |
| 修改路径 Ruff、git diff --check、三份 OpenAPI 一致、43 个 schema 校验 | **PASS** | 本机静态检查 |
| 真实输入流水线运行时代码 SHA、契约 SHA 与当前文件一致 | **PASS** | 完整记录 runtime_sha256 / contract_sha256 |
| Windows NVIDIA 实机与物理多机吞吐 | **NOT_RUN** | 当前主机没有 CUDA 设备 |

P1 保留本机 urllib3/LibreSSL 环境警告，不影响本轮通过。新增自动化测试覆盖提示不占槽、修订号乱序/撤销、TTL、关闭共享与旧会话隔离、CPU 预算、JPEG EXIF 与缓存损坏、尺寸/字节限制、未来任务取消不影响当前模型、六种中断下双进程先停止后确认、P2 重启/丢回包撤销及其他 CPU 可领取已预取任务。

## 真实 OCR 观测

运行目录 `qa/p1-p2/var/input-prefetch-c6be5dbc`。一台 macOS 主机，真实 P1/P2 HTTP、RapidOCR 和 CPU ORT。A 为单槽、1 推理线程、CPU 预算 2、prefetch=1；迁移目标 B 为单槽、1 推理线程、prefetch=0。该 CPU 开关使用与 CUDA 相同的输入流水线。

- 预热后 4 张 3000×3000 真实文字图片全部成功，**3/4 预取命中**；一组连续任务只记录 **1 次模型加载**。
- 用跨进程统一时间戳确认 **3 段下一张输入准备与前一张推理重叠**。阶段耗时仍用单进程单调时钟计算，避免本机 Python 3.9 单调时钟起点不一致导致错误重叠判断。
- 该批次观测墙钟耗时约 **20.30 秒**。这是本机正确性运行（同期存在测试负载），没有配套受控性能基线，**不作为 GPU 提速或 CPU 加速比证据**。
- 取消执行中的 Job 后，其余未执行图片的 attempts 仍为 0，预取缓存被清理。
- 关闭 A 共享时已有一项成功、一项运行、其余等待。B 接续完成后 attempts 为 **1 / 2 / 1 / 1**；运行项以 NODE_UNAVAILABLE 迁移，成功结果保持不变，未执行的预取图片没有额外消耗尝试。
- 同一图片在预取路径和 B 的普通路径产出的完整 OCR JSON **完全一致**。
- 原双槽回归目录为 `qa/p1-p2/var/multislot-cac89e8f`，双进程重叠、模型复用、单槽取消不影响另一槽、全槽关闭后的迁移均通过。

## 复跑

在项目根目录执行：

```sh
(cd P1 && .venv/bin/python -B -m pytest tests/resource -q)
(cd P2 && .venv/bin/python -B -m pytest tests/tasks ../qa/p1-p2/test_run_local.py -q)
P2/.venv/bin/python -B qa/p1-p2/check_input_prefetch.py
P2/.venv/bin/python -B qa/p1-p2/check_multislot.py
```

## Windows GPU 仍须验证

1. 三个模型实际使用所选 CUDA 设备，并用 ORT profiling / gpu_probe 留存 GPU 算子证据。
2. 相同数据、线程预算和模型下比较 prefetch=0/1：正确吞吐、批次耗时、P50/P95、缓存命中、CPU/RAM/VRAM 峰值，分别报告冷启动与预热；同时记录领取间隔差异，不能仅凭一次完成时间归因。
3. 下载/解码与真实 GPU 推理确有重叠，GPU 始终只有一个执行；慢网、大小图、EXIF、缓存未命中/损坏和解码进程崩溃均可恢复。
4. GPU 推理中取消/关闭共享：两个进程退出、Windows 文件句柄和缓存正确清理、显存释放、旧回包拒收；成功结果不变，未完成项迁移到 Mac CPU。
5. 单独取消下一张预取图片所属 Job 不打断其他 Job 的当前 GPU 推理；断网和重启后无重复执行或重复扣尝试。

本阶段未加入跨图片合批、多卡池、CUDA streams、动态内存/显存准入或公平抢占。预取使用主机数组文件，存在校验与磁盘复制开销；小图和高速网络场景可能没有净收益。GPU 吞吐收益以实机对照为准。
