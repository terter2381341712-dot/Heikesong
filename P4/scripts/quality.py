"""Offline OCR quality check. References stay on the acceptance computer."""

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def score(directory, reference_file):
    references = json.loads(reference_file.read_text(encoding="utf-8"))
    if not references.get("frozen_before_run") or not references.get("license"):
        raise ValueError("正式核验需要预先冻结阈值、人工参考与授权说明")
    threshold = references["max_cer"]
    if type(threshold) not in (float, int) or not 0 <= threshold <= 1:
        raise ValueError("max_cer 应为 0–1")
    results = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    if results["task_type"] != "ocr.image.v1":
        raise ValueError("本工具只计 OCR，ASR 需独立质量报告")
    manifest = [
        json.loads(line)
        for line in (directory / "inputs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows, edits, chars, correct = [], 0, 0, 0
    if len(results["items"]) != len(manifest):
        raise ValueError("输入清单与结果数量不一致")
    for item, source in zip(results["items"], manifest):
        if item["input_artifact_id"] != source["input_artifact_id"]:
            raise ValueError("输入与结果不对应")
        truth = normalize(references["texts_by_sha256"][source["input_sha256"]])
        predicted = normalize(item["result"]["text"]) if item["result"] is not None else None
        error = distance(truth, predicted) if predicted is not None else None
        cer = error / len(truth) if truth and error is not None else None
        passed = (
            item["status"] == "SUCCEEDED"
            and predicted is not None
            and (cer <= threshold if truth else predicted == "")
        )
        correct += int(passed)
        if truth:
            chars += len(truth)
            # Failed inputs are not converted to fabricated empty OCR outputs.
            if error is not None:
                edits += error
        rows.append(
            {
                "input_sha256": source["input_sha256"],
                "status": item["status"],
                "reference_chars": len(truth),
                "edits": error,
                "cer": cer,
                "quality_passed": passed,
            }
        )
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    seconds = report.get("observed_job_seconds")
    return {
        "mode": "OFFLINE_REFERENCE_CHECK",
        "normalization": "NFC/LF/strip/whitespace collapse; preserve case and punctuation",
        "max_cer": threshold,
        "items": rows,
        "correct_images": correct,
        "nonempty_observed_cer": edits / chars
        if chars and all(r["edits"] is not None for r in rows if r["reference_chars"])
        else None,
        "observed_correct_images_per_second": correct / seconds if seconds else None,
        "formal_benchmark": "NOT_RUN",
        "quality_set_minimum_met": len(rows) >= 20,
        "notice": "客户端轮询窗口含观测延迟；不替代冻结设备、预热和本地完整执行器正式基准。空参考单列。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--references", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = score(args.run, args.references)
        (args.run / "quality.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"quality checked: {result['correct_images']}/{len(result['items'])}")
        return 0 if all(r["quality_passed"] for r in result["items"]) else 1
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
