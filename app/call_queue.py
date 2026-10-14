"""Bounded FIFO admission for one inference slot and connected waiting callers."""

from collections import OrderedDict

from app.errors import AppError


class CallQueue:
    def __init__(self, capacity=7):
        self.capacity = capacity
        self.active = None
        self.waiting = OrderedDict()

    def position(self, sid):
        if self.active == sid:
            return 0
        return list(self.waiting).index(sid) + 1 if sid in self.waiting else None

    def join(self, sid):
        position = self.position(sid)
        if position is not None:
            return position
        if self.active is None and not self.waiting:
            self.active = sid
            return 0
        if len(self.waiting) >= self.capacity:
            raise AppError("call_queue_full", "Очередь звонков заполнена. Оставьте заявку или попробуйте позже", 429)
        self.waiting[sid] = None
        return len(self.waiting)

    def release(self, sid):
        self.waiting.pop(sid, None)
        if self.active != sid:
            return None
        self.active = next(iter(self.waiting), None)
        self.waiting.pop(self.active, None)
        return self.active

    @property
    def available(self):
        return self.active is None or len(self.waiting) < self.capacity
