"""In-process metrics exposed in Prometheus text format.

Cac chi so do trong tien trinh, xuat theo dinh dang text cua Prometheus.

Cost is tracked alongside latency on purpose: for an LLM service the money spent per
request is an operational signal just as much as how long the request took.
Chi phi duoc theo doi song song voi do tre la co y: voi mot dich vu LLM, so tien tieu
cho moi request cung la mot tin hieu van hanh quan trong khong kem thoi gian xu ly.
"""

import threading
from collections import deque

import numpy as np

# How many recent latencies are kept in order to compute percentiles.
#
# This used to be an unbounded list, which is a slow way to run out of memory: one float was
# appended per request and none was ever removed, so a service that stayed up long enough would
# eventually be storing millions of them, and np.percentile would get slower over every one.
#
# A bounded window is also the more honest measurement. A percentile over every request since the
# process started answers "how has this service ever behaved", which nobody is asking. A
# percentile over the last few thousand answers "how is it behaving now", which is the question a
# latency graph exists for, and it is the one that moves when something breaks.
#
# So bao nhieu do tre gan nhat duoc giu lai de tinh phan vi.
#
# Truoc day day la mot list khong gioi han, va do la mot cach cham rai de het bo nho: moi request
# them mot so thuc va khong bao gio bot di, nen mot dich vu chay du lau se giu hang trieu so, con
# np.percentile thi cham dan theo tung so mot.
#
# Mot cua so co gioi han cung la phep do trung thuc hon. Phan vi tinh tren moi request tu luc tien
# trinh khoi dong tra loi cau "dich vu nay tu truoc toi nay chay ra sao", von khong ai hoi. Phan vi
# tinh tren vai nghin request gan nhat tra loi cau "no dang chay ra sao", dung cau ma mot bieu do
# do tre sinh ra de tra loi, va la cau se doi ngay khi co su co.
LATENCY_WINDOW = 10_000


class Metrics:
    def __init__(self, *, latency_window: int = LATENCY_WINDOW) -> None:
        self._lock = threading.Lock()
        self._latencies_ms: deque[float] = deque(maxlen=latency_window)
        self._requests = 0
        self._errors = 0
        self._tool_calls: dict[str, int] = {}
        self._tool_denied = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0

    def record_request(
        self,
        *,
        latency_ms: float,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            self._requests += 1
            self._latencies_ms.append(latency_ms)
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._cost_usd += cost_usd

    def record_error(self) -> None:
        with self._lock:
            self._errors += 1

    def record_tool_call(self, name: str, *, allowed: bool) -> None:
        with self._lock:
            self._tool_calls[name] = self._tool_calls.get(name, 0) + 1
            if not allowed:
                self._tool_denied += 1

    def snapshot(self) -> dict:
        with self._lock:
            latencies = np.asarray(self._latencies_ms, dtype=np.float64)
            return {
                "requests": self._requests,
                "errors": self._errors,
                "tool_calls": dict(self._tool_calls),
                "tool_denied": self._tool_denied,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
                "cost_usd": round(self._cost_usd, 6),
                "latency_ms": {
                    "p50": _percentile(latencies, 50),
                    "p95": _percentile(latencies, 95),
                    "p99": _percentile(latencies, 99),
                    "avg": round(float(latencies.mean()), 1) if latencies.size else 0.0,
                },
            }

    def render_prometheus(self) -> str:
        snap = self.snapshot()
        lines = [
            "# HELP agent_requests_total So request da xu ly.",
            "# TYPE agent_requests_total counter",
            f"agent_requests_total {snap['requests']}",
            "# HELP agent_errors_total So request bi loi.",
            "# TYPE agent_errors_total counter",
            f"agent_errors_total {snap['errors']}",
            "# HELP agent_tool_denied_total So lan guardrail chan mot lenh goi tool.",
            "# TYPE agent_tool_denied_total counter",
            f"agent_tool_denied_total {snap['tool_denied']}",
            "# HELP agent_tokens_total Tong so token da dung.",
            "# TYPE agent_tokens_total counter",
            f'agent_tokens_total{{direction="input"}} {snap["input_tokens"]}',
            f'agent_tokens_total{{direction="output"}} {snap["output_tokens"]}',
            "# HELP agent_cost_usd_total Chi phi uoc tinh (USD).",
            "# TYPE agent_cost_usd_total counter",
            f"agent_cost_usd_total {snap['cost_usd']}",
            "# HELP agent_latency_ms Do tre xu ly request (mili giay).",
            "# TYPE agent_latency_ms summary",
            f'agent_latency_ms{{quantile="0.5"}} {snap["latency_ms"]["p50"]}',
            f'agent_latency_ms{{quantile="0.95"}} {snap["latency_ms"]["p95"]}',
            f'agent_latency_ms{{quantile="0.99"}} {snap["latency_ms"]["p99"]}',
        ]
        if snap["tool_calls"]:
            lines.append("# HELP agent_tool_calls_total So lan moi tool duoc goi.")
            lines.append("# TYPE agent_tool_calls_total counter")
            for name, count in sorted(snap["tool_calls"].items()):
                lines.append(f'agent_tool_calls_total{{tool="{name}"}} {count}')
        return "\n".join(lines) + "\n"


def _percentile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        return 0.0
    return round(float(np.percentile(values, q)), 1)


metrics = Metrics()
