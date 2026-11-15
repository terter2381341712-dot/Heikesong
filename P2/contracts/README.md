# v0.2.0 契约来源

`openapi.yaml` 是团队执行包 `D题参赛执行包/contracts/openapi.yaml` 的逐字节副本，未修改字段、状态、枚举或接口版本。公共执行包保留在项目根目录，P2 在本目录携带运行所需的契约副本，避免依赖某位开发者的执行包绝对路径。

- 来源工作区：`/Users/zhouyuechen/Documents/黑客松/D题参赛执行包/contracts/openapi.yaml`
- SHA-256：`376f565d7077ae5de41a0270c1c8c6c1952dc1526b0bc4219ee2f6b8b5b93a31`
- 测试样例副本：`tests/tasks/fixtures/contract-examples.json`
- 样例来源：同一执行包的 `contracts/examples.json`
- 样例 SHA-256：`bd73fa147bbc92bfc55593f8f837fdff0337a0bbd5e59f0ac2747d38a51c2f70`

P2 直接加载此 OpenAPI 的 JSON Schema 校验请求、P1 响应、OCR 结果和对外响应，避免另建一套字段定义。运行时不访问来源工作区。测试样例位于 `P2/tests/tasks/fixtures/contract-examples.json`；模拟服务的合成数据不能作为模型配置、自测或多机性能证据。

共享契约变更由 P4 汇总并与提供方、调用方确认后再同步；不要为适配本地实现单方面修改此快照。ASR 等定义保留，但 P2 本次只开放实现了拆分和汇总的 OCR 类型。

## 统一计划 1.0 的语义补充

依据 [统一计划](../../归档/P1-P2统一修改与联调计划.md) 第 3、5 节，本轮由 P2 负责人汇总共享文档与样例，P1 核对资源侧语义，P4 参与时统一发布。公共路径、字段、状态、HTTP 码和 schema 均未变更，以上 OpenAPI 与原样例哈希保持不变。

- 补充来源：项目根目录 `qa/p1-p2/capabilities.json`，与计划第 3.4 节一致的六种完整 Capability 样例。
- P2 副本：`tests/tasks/fixtures/p1-p2-capabilities.json`；逐字节一致，SHA-256 为 `146c12e35cb865db8a45425fe60f32c3c066fd1474fe1295accd58daa719fa1d`。
- 两种 NOT_PREPARED、PREPARING、READY、ERROR、自述不兼容的 UNAVAILABLE 都按原 `Capability` schema 校验。READY 缺证据等语义负例由 P2 协议测试构造；schema 合法不代表可以执行。
- 模拟准备完成时报告请求中的目标摘要及合成自测/后端信息，不把初始 null 沿用为 READY 摘要。样例不包含真实权重、自测或物理设备证据。
