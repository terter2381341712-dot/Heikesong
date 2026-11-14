# v0.2.0 契约来源

`openapi.yaml` 是团队执行包 `D题参赛执行包/contracts/openapi.yaml` 的逐字节副本，未修改字段、状态、枚举或接口版本。当前工作目录整理为独立 P2 仓库后，将运行所需的契约单独放在这里，避免依赖某位开发者的执行包绝对路径。

- 来源工作区：`/Users/zhouyuechen/Documents/黑客松/D题参赛执行包/contracts/openapi.yaml`
- SHA-256：`376f565d7077ae5de41a0270c1c8c6c1952dc1526b0bc4219ee2f6b8b5b93a31`
- 测试样例副本：`tests/tasks/fixtures/contract-examples.json`
- 样例来源：同一执行包的 `contracts/examples.json`
- 样例 SHA-256：`bd73fa147bbc92bfc55593f8f837fdff0337a0bbd5e59f0ac2747d38a51c2f70`

P2 直接加载此 OpenAPI 的 JSON Schema 校验请求、P1 响应、OCR 结果和对外响应，避免另建一套字段定义。运行时不访问来源工作区。模拟服务的合成数据仅存在于测试目录，不能作为模型配置、自测或多机性能证据。

共享契约变更由 P4 汇总并与提供方、调用方确认后再同步；不要为适配本地实现单方面修改此快照。ASR 等定义保留，但 P2 本次只开放实现了拆分和汇总的 OCR 类型。
