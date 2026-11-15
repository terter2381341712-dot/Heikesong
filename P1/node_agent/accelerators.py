"""Enumerate physical adapters, retaining the actual backend device ordinal."""

import ctypes
import json
import platform
import subprocess
import uuid


def windows_adapters():
    from ctypes import wintypes as w

    class Guid(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    class Luid(ctypes.Structure):
        _fields_ = [("low", w.DWORD), ("high", w.LONG)]

    class Desc(ctypes.Structure):
        _fields_ = [("name", w.WCHAR * 128), ("vendor", w.UINT), ("device", w.UINT),
                    ("subsys", w.UINT), ("revision", w.UINT), ("dedicated", ctypes.c_size_t),
                    ("system", ctypes.c_size_t), ("shared", ctypes.c_size_t),
                    ("luid", Luid), ("flags", w.UINT)]

    def method(pointer, slot, result, *arguments):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result, ctypes.c_void_p, *arguments)(table[slot])

    factory = ctypes.c_void_p()
    iid = Guid.from_buffer_copy(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
    create = ctypes.WinDLL("dxgi").CreateDXGIFactory1
    create.argtypes = [ctypes.POINTER(Guid), ctypes.POINTER(ctypes.c_void_p)]
    create.restype = w.LONG
    if create(ctypes.byref(iid), ctypes.byref(factory)) < 0:
        raise OSError("DXGI adapter enumeration failed")
    result = []
    try:
        enum = method(factory, 12, w.LONG, w.UINT, ctypes.POINTER(ctypes.c_void_p))
        for index in range(32):
            adapter = ctypes.c_void_p()
            status = enum(factory, index, ctypes.byref(adapter))
            if status == -2005270526:  # DXGI_ERROR_NOT_FOUND
                break
            if status < 0:
                raise OSError(f"DXGI enumeration error {status}")
            try:
                desc = Desc()
                if method(adapter, 10, w.LONG, ctypes.POINTER(Desc))(adapter, ctypes.byref(desc)) < 0:
                    raise OSError("DXGI adapter description failed")
                if not desc.flags & 2:  # Exclude software/WARP; do not renumber hardware IDs.
                    result.append({"backend": "directml", "device_id": index, "name": desc.name,
                                   "vendor_id": desc.vendor, "directml_device_id": index,
                                   "luid": f"{desc.luid.high & 0xffffffff:08x}_{desc.luid.low:08x}",
                                   "dedicated_bytes": desc.dedicated, "shared_bytes": desc.shared})
            finally:
                method(adapter, 2, w.ULONG)(adapter)
    finally:
        method(factory, 2, w.ULONG)(factory)
    return result


def discover():
    system = platform.system()
    if system == "Windows":
        try:
            adapters = windows_adapters()
        except (OSError, AttributeError):
            adapters = []
        try:
            reply = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                                   capture_output=True, text=True, timeout=10, check=True)
            cuda = [(int(index.strip()), name.strip()) for index, name in
                    (line.split(",", 1) for line in reply.stdout.splitlines() if line.strip())]
            nvidia = [a for a in adapters if a["vendor_id"] == 0x10de]
            # Match unambiguous names, never equate a DXGI ordinal to CUDA's ordinal.
            for adapter in adapters:
                matches = [(index, name) for index, name in cuda if name.casefold() == adapter["name"].casefold()]
                if adapter["vendor_id"] == 0x10de and len(matches) == 1:
                    adapter.update(backend="cuda", device_id=matches[0][0])
                elif adapter["vendor_id"] == 0x10de and len(nvidia) == len(cuda) == 1:
                    adapter.update(backend="cuda", device_id=cuda[0][0])
            if not nvidia:
                adapters.extend({"backend": "cuda", "device_id": index, "name": name} for index, name in cuda)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # DirectML still addresses every enumerated Windows adapter.
        return adapters
    if system == "Darwin":
        # CoreML selects Apple hardware itself; it has no per-GPU device_id API.
        reply = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"],
                               capture_output=True, text=True, timeout=15, check=True)
        devices = json.loads(reply.stdout).get("SPDisplaysDataType", [])
        return [{"backend": "coreml", "device_id": 0,
                 "name": ", ".join(d.get("sppci_model", d.get("_name", "Apple GPU")) for d in devices)}] if devices else []
    reply = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=10, check=True)
    return [{"backend": "cuda", "device_id": int(index.strip()), "name": name.strip()}
            for index, name in (line.split(",", 1) for line in reply.stdout.splitlines() if line.strip())]


if __name__ == "__main__":
    print(json.dumps(discover(), ensure_ascii=True))
