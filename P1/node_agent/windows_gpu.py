"""Per-adapter GPU engine utilization via English PDH counters."""
import ctypes
import re
from collections import defaultdict

_counter = None


class Counter:
    def __init__(self):
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL("pdh")
        self.query, self.counter = ctypes.c_void_p(), ctypes.c_void_p()
        self.api.PdhOpenQueryW.argtypes = [w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self.api.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, w.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self.api.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        self.api.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        if self.api.PdhOpenQueryW(None, 0, ctypes.byref(self.query)):
            raise OSError("Cannot open GPU PDH query")
        if self.api.PdhAddEnglishCounterW(self.query, r"\GPU Engine(*)\Utilization Percentage", 0, ctypes.byref(self.counter)):
            raise OSError("GPU engine performance counters unavailable")
        self.api.PdhCollectQueryData(self.query)

    def values(self):
        from ctypes import wintypes as w
        class Value(ctypes.Structure):
            _fields_ = [("status", w.DWORD), ("value", ctypes.c_double)]
        class Item(ctypes.Structure):
            _fields_ = [("name", w.LPWSTR), ("value", Value)]
        if self.api.PdhCollectQueryData(self.query):
            return []
        size, count = w.DWORD(), w.DWORD()
        self.api.PdhGetFormattedCounterArrayW(self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), None)
        if not size.value:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if self.api.PdhGetFormattedCounterArrayW(self.counter, 0x200, ctypes.byref(size), ctypes.byref(count), buffer):
            return []
        items = ctypes.cast(buffer, ctypes.POINTER(Item))
        return [(items[i].name, items[i].value.value) for i in range(count.value) if items[i].value.status in (0, 1)]


def aggregate(values, luid):
    engines = defaultdict(float)
    for name, value in values:
        match = re.search(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_\d+_eng_(\d+)", name, re.I)
        if match and f"{int(match[1], 16):08x}_{int(match[2], 16):08x}" == luid.lower():
            engines[match[3]] += max(0, value)
    return min(100, max(engines.values())) if engines else None


def utilization(luid):
    global _counter
    if not luid:
        return None
    if _counter is None:
        _counter = Counter()
    return aggregate(_counter.values(), luid)
