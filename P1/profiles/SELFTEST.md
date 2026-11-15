# ocr.default.v1 自测记录（发布依据）

- 原始自测日期：2026-11-14（北京时间），设备：MacBook Pro M4 16GB，macOS，Python 3.9.6
- 2026-11-15 版本统一：解释器改为 Python 3.12.15，重新核验组合 SHA256 仍为 `3358ac6b…0edc5`，模型权重字节未变。下表耗时来自 3.9.6 记录，仅作历史参考。
- 执行器：`runners/rapidocr_runner.py`，backend=cpu，runtime_provider=CPUExecutionProvider
- 权重：rapidocr==3.10.0 内置 PP-OCRv6 det/cls/rec，组合 SHA256
  `3358ac6b2a0919cf5c72159265440ba62fb496b504d1fc6fb92a4ffc4fc0edc5`
- 许可核对：RapidOCR Apache-2.0；onnxruntime MIT；Pillow HPND/MIT-CMU；
  PP-OCR 权重 Apache-2.0（PaddleOCR 官方发布）。`weights_license_verified=true`。

## 合成样本试跑（预热后）

| 样本 | 期望文本 | 识别结果 | confidence | 耗时 ms |
|---|---|---|---|---|
| sample-0 480×160 | HELLO OCR TEST 123 | HELLO OCR TEST 123 | 0.98026 | 308.5 |
| sample-1 640×240 | APPROVED 456 | APPROVED 456 | 0.99987 | 257.2 |
| sample-2 360×200 | SAMPLE 789 | SAMPLE 789 | 0.99301 | 185.0 |

中位数 257.2 ms；三张全部命中，bbox 在图像范围内。**该数据为开发机自测记录，
不是正式性能测量**（正式单机/多机对照由 P4 按冻结口径执行）。

## 发布检查

- [x] 锁文件不含自身 profile_digest
- [x] 依赖版本与 requirements.txt 一致
- [x] 输出通过 resultschema.validate_ocr_result
- [x] 引擎实例进程内复用
