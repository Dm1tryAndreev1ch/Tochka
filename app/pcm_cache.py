"""Bounded in-memory audio for explicitly cacheable public/system replies."""

from collections import OrderedDict


class PCMCache:
    def __init__(self, max_bytes=8 * 1024 * 1024, max_entries=128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def get(self, key):
        value = self.entries.get(key)
        if value is None:
            self.misses += 1
            return None
        self.entries.move_to_end(key)
        self.hits += 1
        return value

    def put(self, key, audio):
        if not audio or len(audio) > self.max_bytes or self.max_entries <= 0:
            return
        old = self.entries.pop(key, None)
        if old is not None:
            self.bytes -= len(old)
        self.entries[key] = audio
        self.bytes += len(audio)
        while self.bytes > self.max_bytes or len(self.entries) > self.max_entries:
            _, removed = self.entries.popitem(last=False)
            self.bytes -= len(removed)

    def clear(self):
        self.entries.clear()
        self.bytes = 0

    def snapshot(self):
        return {"entries": len(self.entries), "bytes": self.bytes, "hits": self.hits, "misses": self.misses}
