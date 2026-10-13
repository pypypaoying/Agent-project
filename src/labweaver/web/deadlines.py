"""Monotonic supervision independent of socket timeouts and progress heartbeats."""

import math
import time


class RunDeadline:
    def __init__(self, model_timeout, task_timeout, *, clock=time.monotonic):
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0
               for v in (model_timeout, task_timeout)):
            raise ValueError("Run deadlines must be finite positive seconds")
        self.model_timeout, self.task_timeout, self.clock = model_timeout, task_timeout, clock
        self.elapsed = 0.0
        self.started = clock()
        self.models = {}

    def pause(self):
        if self.started is not None:
            self.elapsed += self.clock() - self.started
            self.started = None
        self.models.clear()

    def resume(self):
        if self.started is None:
            self.started = self.clock()

    def observe(self, kind, payload):
        key = (payload.get("role"), payload.get("parent_call_id"), payload.get("call"))
        if kind == "model_start" and self.started is not None:
            # Duplicate start events must not extend the same request's deadline.
            self.models.setdefault(key, self.clock())
        elif kind == "model_end":
            self.models.pop(key, None)

    def error(self):
        if self.started is None:
            return None
        current = self.clock()
        if self.elapsed + current - self.started >= self.task_timeout:
            return {"code": "task_timeout", "limit_seconds": self.task_timeout}
        for (role, parent_call_id, call), start in self.models.items():
            if current - start >= self.model_timeout:
                return {"code": "model_request_timeout", "limit_seconds": self.model_timeout,
                        "role": role, "call": call, "parent_call_id": parent_call_id}
        return None
