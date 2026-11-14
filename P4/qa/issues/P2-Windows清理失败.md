# P2-WIN-002：HTTP进程自测的临时目录清理失败

状态：FAIL，原因待 P2/测试入口负责人确认。当前不归结为业务调度失败。

环境 Windows、Python 3.13.7、依赖见 test-environment.txt。P2全量101项：100通过，1失败。失败项 `test_http_processes.py::test_real_http_process_start_restart_and_offline_download` 在退出 TemporaryDirectory 时触发 WinError32，p2-data/p2.lock 被占用。完整原始 JUnit 在 `../p2-pytest.xml`。

将 TEMP/TMP 改为项目内目录、关闭代理后独立复现，同样失败，见 `../p2-http-pytest.xml`。额外试过在 P4 临时适配器用 taskkill /T 结束子进程树，仍然出现同一错误，尚无充分证据认定根因，未交付该失败适配器为解决方案。

保留的运行日志显示建Job、三输入成功、取消、P2重启、P1离线后结果重放/ZIP都返回预期状态；但整体测试仍然是 FAIL，不能把业务步骤完成改成整项PASS。原P2源码和测试均未修改。

复现命令（从P2目录，先按P2 README安装测试依赖）：

```powershell
$env:TEMP = (Resolve-Path ../P4/var/tmp-p2).Path
$env:TMP = $env:TEMP
$env:NO_PROXY = '*'
../P4/.venv/Scripts/python.exe -m pytest -q tests/tasks/test_http_processes.py --basetemp=../P4/var/pytest-p2-smoke-new --junitxml=../P4/var/p2-http-new.xml
```

首次需要创建 tmp-p2；失败留下目录供定位，不强制删除占用文件。交接时检查停止进程后锁文件句柄与临时目录清理行为，再回归全量，不用忽略清理异常或降低测试断言签收。
