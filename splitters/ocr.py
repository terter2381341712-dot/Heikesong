from task_service.contract import APIError, uid

TASK_TYPE = "ocr.image.v1"


def split_images(artifacts, descriptor):
    seen = set()
    tasks = []
    for index, artifact in enumerate(artifacts):
        info = artifact["media_info"]
        if (artifact["kind"] != "INPUT"
                or artifact["content_type"] not in {"image/jpeg", "image/png"}
                or artifact["content_type"] not in descriptor["input_mime_types"]
                or not info["width"] or not info["height"]):
            raise APIError(422, "INPUT_INVALID", "OCR 只接受具有有效尺寸的 JPG/PNG 输入",
                           artifact_id=artifact["artifact_id"])
        if (artifact["size_bytes"] > min(10485760, descriptor["max_file_bytes"])
                or info["width"] * info["height"] > min(20000000, descriptor["max_pixels"] or 20000000)):
            raise APIError(413, "INPUT_TOO_LARGE", "图片超过字节或像素限制",
                           artifact_id=artifact["artifact_id"])
        if artifact["sha256"] in seen:
            raise APIError(422, "INPUT_INVALID", "同一 Job 不能包含原字节 SHA 重复的图片",
                           artifact_id=artifact["artifact_id"])
        seen.add(artifact["sha256"])
        tasks.append({
            "task_id": uid(), "input_index": index, "input": artifact, "status": "QUEUED",
            "attempts_used": 0, "current_execution_id": None, "dispatches": [],
            "result_artifact": None, "waiting_reason": None, "error": None,
        })
    return tasks
