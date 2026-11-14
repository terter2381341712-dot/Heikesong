# P4-VENV-001：解析解释器符号链接后混用系统环境

状态：启动器已修复，Mac实机待用户同步后复验。本次只修改P4。

用户在Mac确认P1/.venv有RapidOCR，系统Python只有新安装的P2依赖。两者都为Python3.9.6，但P4使用Path.resolve保存解释器，跟随venv/bin/python符号链接切换到系统Python。Python版本相同不能证明sys.prefix、site-packages或解释器入口相同，先前Windows单机闭环没有覆盖这个POSIX差异。

修复：cluster.absolute_interpreter使用os.path.abspath，只取绝对路径，不跟随符号链接。control.py的候选去重、已有/新建环境保存、P2复用及安装目标均使用此规则；cluster.py的机器JSON解释器字段、预检输出、模块命令和实际启动也使用同一规则。源码根目录和数据目录的resolve保留，不做全局替换。联合检查失败时显示实际解释器路径供排查。

回归：venv-path-pytest.xml记录45 PASS / 1 SKIP。模拟链接的测试明确将venv入口resolve到另一个system路径，覆盖选择、P2安装/联合检查、新环境保存、机器配置、plan和Popen参数。另附真实POSIX symlink venv测试，校验sys.prefix和仅存在于虚拟环境的模块；当前Windows执行该项SKIP，没有声称完成Mac实测。

同步修复后，在Mac旧控制台终端按Ctrl+C退出，再执行 `bash P4/start.command`。选择中心时会用P1/.venv的原入口补齐P2，不再用该链接指向的系统Python。已经装进系统的P2包无需为这次修复卸载。若只加入计算节点，仍只需要P1环境。

Mac可额外从项目根执行：

```sh
P1/.venv/bin/python -m pytest -q P4/tests/test_interpreter_paths.py
```

该命令在Mac上会执行真实POSIX虚拟环境测试。日常按钮操作无需手动运行此测试，也无需安装Python3.11。
