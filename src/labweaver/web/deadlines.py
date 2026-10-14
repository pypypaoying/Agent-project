"""Monotonic supervision independent of socket timeouts and progress heartbeats."""

import math
import time
from labweaver.runtime.timing import TaskTiming


class RunDeadline(TaskTiming):
    def __init__(self, model_timeout, task_timeout, *, clock=time.monotonic):
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0
               for v in (model_timeout, task_timeout)):
            raise ValueError("Run deadlines must be finite positive seconds")
        self.model_timeout, self.task_timeout = model_timeout, task_timeout
        super().__init__(clock=clock, source="host_monotonic")

    def error(self):
        # Cancellation can finish pending requests from another thread. Share
        # the timing lock instead of iterating a concurrently cleared mapping.
        with self._lock:
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
