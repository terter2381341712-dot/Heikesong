"""文件元信息探测：图片宽高（EXIF 方向纠正）、WAV 时长/采样率/通道。未知值为 null，不用 0。"""
from __future__ import annotations

import io
import wave

from PIL import Image, UnidentifiedImageError

# EXIF 方向 5-8 表示旋转 90°，报告坐标空间需交换宽高
_SWAP_ORIENTATIONS = {5, 6, 7, 8}


def _null_media() -> dict:
    return {
        "width": None,
        "height": None,
        "duration_ms": None,
        "sample_rate_hz": None,
        "channels": None,
        "audio_encoding": None,
    }


def probe_media(content_type: str, data: bytes) -> dict:
    if content_type in ("image/jpeg", "image/png"):
        return probe_image(data, content_type)
    if content_type == "audio/wav":
        return probe_wav(data)
    return _null_media()


def probe_image(data: bytes, content_type: str) -> dict:
    media = _null_media()
    try:
        with Image.open(io.BytesIO(data)) as img:
            if Image.MIME.get(img.format) != content_type:
                raise ValueError("IMAGE_TYPE_MISMATCH")
            orientation = 1
            exif = img.getexif()
            if exif:
                orientation = int(exif.get(274, 1) or 1)
            width, height = img.size
            if orientation in _SWAP_ORIENTATIONS:
                width, height = height, width
            media["width"] = int(width)
            media["height"] = int(height)
        # 重新打开以便 Pillow 在文件开头校验内容。
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValueError("IMAGE_UNDECODABLE") from exc
    return media


def probe_wav(data: bytes) -> dict:
    media = _null_media()
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            if wav.getsampwidth() != 2 or frames < 1:
                raise ValueError("WAV 必须包含 16-bit PCM 样本")
            if len(wav.readframes(frames)) != frames * wav.getnchannels() * 2:
                raise ValueError("WAV 音频数据不完整")
            media["duration_ms"] = int(frames * 1000 / rate) if rate > 0 else None
            media["sample_rate_hz"] = rate or None
            media["channels"] = wav.getnchannels() or None
            media["audio_encoding"] = "PCM_S16LE"
    except (wave.Error, OSError, EOFError):
        raise ValueError("WAV_UNDECODABLE")
    return media
