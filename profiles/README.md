# profiles/ —— P1 专属目录

运行时配置由 resource_service 从本目录读取：

- `task_types.json`：任务类型目录（运行时副本，来源为共享契约 `contracts/task-types.json`）。
- `<profile_id>.lock.json`：**profile 锁文件**。资源服务为支持且锁文件通过检查的类型标记 `CONFIGURED`，以锁文件原始字节的 SHA-256 作为 `profile_digest`。

## 发布一个 profile 的流程

1. 选定模型与依赖，写入锁文件（参考 `contracts/runtime-manifest.example.json` 的 RuntimeProfile 结构）。
2. 在本机自测通过（校准图片跑通、输出符合 result schema）。
3. 把 `<profile_id>.lock.json` 放进本目录，重启或调用 catalog reload。
4. `GET /api/resource/v1/task-types` 应显示 `CONFIGURED` 与 64 位 digest；通知 P2/P4。

## 纪律

- 锁文件内**不放自身 profile_digest**（digest 由服务端对原字节计算）。
- 未实测通过不得发布；`verified=true` 只能来自真实自测。
- 模型权重放 `models/`（已 gitignore），锁文件记录来源、许可与 SHA256。
