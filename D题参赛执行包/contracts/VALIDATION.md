# 契约检查记录

检查日期：2026-11-14。

- OpenAPI 3.1 规范校验：PASS（openapi-spec-validator）。
- 13 个端点、13 个唯一 operationId；路径参数与鉴权方案引用一致。
- 31 个数据 schema：JSON Schema Draft 2020-12 结构检查通过。
- 全部本地 $ref 可解析。
- 19 份请求/响应样例通过相应 schema 与日期/UUID 格式检查。
- 文档内部链接已检查；原赛题文件不在当前目录，其引用改为明确标记的已读来源记录。
- 样例 Job 的总数与分状态计数一致。

这只验证计划与数据契约结构，不验证运行时行为、竞态安全、模型质量、设备兼容性或真实性能。应用尚未实现；验收 A01–A20 均待团队执行。合成样例不能作为比赛性能证据。

原始检查汇总见 [validation-results.json](validation-results.json)。
