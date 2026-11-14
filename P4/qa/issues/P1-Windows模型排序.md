# P1-WIN-001：Windows 的模型组合 SHA 与锁文件不一致

**后续状态：已修复。** 用户进一步要求按钮化自动跨系统运行后，已把明确文件名排序正式纳入P1源码并增加PureWindowsPath/PurePosixPath顺序回归；不再需要用户执行下文历史patch命令。按钮真实单机OCR闭环通过；当前回归见p1-buttons-pytest.xml。以下保留早期隔离验证和交接过程。

状态：生产 P1 未修复；最小补丁已在隔离副本验证。负责人：P1。不依赖第二台设备。

实际环境 Python 3.9.6、RapidOCR 3.10.0、ONNX Runtime 1.19.2、Pillow 11.3.0、Windows。模型文件未改变。源码 `P1/runners/rapidocr_runner.py` 的 model_files 使用 `sorted(Path...)`；WindowsPath 按不区分大小写的路径语义比较，Linux PosixPath 顺序不同。

| 顺序 | 组合 SHA（文件名UTF-8+原字节） |
|---|---|
| Windows默认：cls → det → rec | 40f92f75aacd8c6da6a49962f4f30d3046d0d1d9ddc756a01041e175b96b92f3 |
| 按文件名明确排序：det → rec → cls | 3358ac6b2a0919cf5c72159265440ba62fb496b504d1fc6fb92a4ffc4fc0edc5 |

第二行与当前锁文件 model_sha256 完全一致。复现：在 P1 环境运行以下代码，第一项为当前程序摘要，第二项为明确排序摘要：

```python
import hashlib
from runners.rapidocr_runner import model_files, combined_model_sha256
print(combined_model_sha256())
h = hashlib.sha256()
for path in sorted(model_files(), key=lambda p: p.name):
    h.update(path.name.encode('utf-8'))
    h.update(path.read_bytes())
print(h.hexdigest())
```

实际三个文件 SHA：

- PP-OCRv6_det_small.onnx：090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f
- PP-OCRv6_rec_small.onnx：6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884
- ch_ppocr_mobile_v2.0_cls_mobile.onnx：e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c

原代码真实实验：节点注册成功，准备 ERROR / 模型SHA不符，Job等待；证据 `../original-p1-ocr-failure.json`。复现一次，并用上面两个排序摘要确认原因。

补丁 [P1-windows-model-order.patch](P1-windows-model-order.patch) 只将排序 key 设为文件名字符串，不改模型、锁、摘要算法、依赖或成功门槛；已 `git apply --check`。按 P4 任务卡交 P1 负责人在项目根目录应用：

```powershell
git apply --check P4/qa/issues/P1-windows-model-order.patch
git apply P4/qa/issues/P1-windows-model-order.patch
```

P4 没有执行第二条来修改原模块。已在 `P4/var/p1-candidate` 隔离副本应用：P1 全部55项回归通过；真实单节点OCR两张图分别输出 HELLO OCR 123、APPROVED 456，Task/执行/结果SHA/ZIP关联通过。见 `../p1-candidate-pytest.xml`、`../candidate-ocr/`。这是候选补丁证据，不能说生产源码已经通过。

负责人应用后需重新跑 P1 全量、P4真实闭环，再做物理两机。Windows测试依赖安装过程中构建antlr wheel需可写缓存；本轮缓存放P4/var，没有修改模型环境校验。
