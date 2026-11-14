import io
import zipfile

from task_service.contract import APIError, JOB_TERMINAL, canonical


def results(state):
    job = state["job"]
    items = [{
        "input_index": task["input_index"], "task_id": task["task_id"],
        "input_artifact_id": task["input"]["artifact_id"],
        "original_name": task["input"]["original_name"], "status": task["status"],
        "result": state["results"].get(task["task_id"]), "error": task["error"],
    } for task in job["tasks"]]
    return {"job_id": job["job_id"], "task_type": job["task_type"], "status": job["status"],
            "items": items, "combined_text": "\n\n".join(
                item["result"]["text"] for item in items if item["status"] == "SUCCEEDED")}


def archive(state):
    job = state["job"]
    if job["status"] not in JOB_TERMINAL:
        raise APIError(409, "JOB_NOT_TERMINAL", "Job 尚未结束", job_id=job["job_id"])
    combined = results(state)
    # Export only public contract objects, never internal requests/config/tokens/leases.
    manifest = {"contract_version": "0.2.0", "job": job}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr("manifest.json", canonical(manifest))
        output.writestr("results.json", canonical(combined))
        output.writestr("combined.txt", combined["combined_text"])
        for item in combined["items"]:
            if item["status"] == "SUCCEEDED":
                path = f"results/{item['input_index']:04d}-{item['task_id']}.json"
                output.writestr(path, canonical(item["result"]))
    return buffer.getvalue()
