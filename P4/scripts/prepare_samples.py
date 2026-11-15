"""Generate reproducible synthetic OCR candidates, never pretend human references are frozen."""

import argparse
import hashlib
import json
from pathlib import Path


def main():
    from PIL import Image, ImageDraw, ImageFont

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    images = args.out / "images"
    images.mkdir()
    texts = [f"CAMPUS OCR {i:03d}\nTASK {1000+i} APPROVED" for i in range(22)] + ["", ""]
    rows, references = [], {}
    for index, text in enumerate(texts):
        size = 28 + (index % 4) * 8
        im = Image.new("RGB", (900, 300), "white")
        ImageDraw.Draw(im).multiline_text((35, 60), text, font=ImageFont.load_default(size=size), fill="black", spacing=12)
        if index % 5 == 0:
            im = im.rotate(2, fillcolor="white")
        name = f"sample-{index:02d}." + ("jpg" if index % 2 else "png")
        im.save(images / name)
        sha = hashlib.sha256((images / name).read_bytes()).hexdigest()
        references[sha] = text
        rows.append({"file": "images/" + name, "sha256": sha, "candidate_reference": text,
                     "source": "synthetic locally generated", "human_checked": False,
                     "category": "blank" if not text else "rotated" if index % 5 == 0 else "clean"})
    (args.out / "samples.json").write_text(json.dumps(rows, indent=2) + "\n")
    (args.out / "references.json").write_text(json.dumps({"frozen_before_run": False,
        "license": "Locally generated synthetic text; add source/license records for any added real images.",
        "max_cer": None, "texts_by_sha256": references}, indent=2) + "\n")
    (args.out / "README.md").write_text("# 候选验收样本\n\n24 张合成 PNG/JPG，包含旋转与空文本，只用于冒烟测试。\n"
        "正式吞吐量/质量验收前，由两位队友逐图核对参考文本，补充中文、多语言及真实困难图片；"
        "设定 max_cer，再将 frozen_before_run 改为 true，保留冻结时间与文件 SHA。"
        "未冻结时只报告完成吞吐量，不报告正确吞吐量或正式验收通过。\n")
    print(args.out)


if __name__ == "__main__":
    main()
