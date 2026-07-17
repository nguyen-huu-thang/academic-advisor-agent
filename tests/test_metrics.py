"""Tests for latency, token and cost accounting.

Kiểm thử việc đo độ trễ, số token và chi phí.
"""

from app.config import estimate_cost_usd
from app.observability.metrics import Metrics


def test_cost_matches_the_published_price_list():
    # 1M input + 1M output on gemini-2.5-flash costs 0.30 + 2.50 USD.
    # 1 triệu token input + 1 triệu token output trên gemini-2.5-flash tốn 0,30 + 2,50 USD.
    cost = estimate_cost_usd("gemini-2.5-flash", 1_000_000, 1_000_000)
    assert cost == 0.30 + 2.50


def test_unknown_model_costs_nothing_rather_than_crashing():
    assert estimate_cost_usd("model-khong-ton-tai", 1000, 1000) == 0.0


def test_percentiles_are_reported():
    metrics = Metrics()
    for latency in (100, 200, 300, 400, 5000):
        metrics.record_request(
            latency_ms=latency, input_tokens=10, output_tokens=5, cost_usd=0.001
        )

    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 5
    assert snapshot["input_tokens"] == 50
    assert snapshot["output_tokens"] == 25
    assert snapshot["latency_ms"]["p50"] == 300
    assert snapshot["latency_ms"]["p95"] > snapshot["latency_ms"]["p50"]


def test_denied_tool_calls_are_counted_separately():
    metrics = Metrics()
    metrics.record_tool_call("tra_cuu_so_du", allowed=True)
    metrics.record_tool_call("chuyen_tien", allowed=False)

    snapshot = metrics.snapshot()
    assert snapshot["tool_calls"] == {"tra_cuu_so_du": 1, "chuyen_tien": 1}
    assert snapshot["tool_denied"] == 1


def test_latency_history_does_not_grow_without_bound():
    """The counters stay exact; only the latency window is bounded.

    Các bộ đếm vẫn chính xác tuyệt đối; chỉ có cửa sổ độ trễ là bị giới hạn.

    This used to be an unbounded list: one float per request, kept for ever. A service that
    stayed up long enough would run out of memory by measuring itself, and the percentile
    calculation would get slower with every request it had ever served.
    Trước đây đây là một list không giới hạn: mỗi request một số thực, giữ lại mãi mãi. Một dịch vụ
    chạy đủ lâu sẽ hết bộ nhớ chỉ vì tự đo chính mình, và phép tính phân vị sẽ chậm dần theo từng
    request nó từng phục vụ.
    """
    metrics = Metrics(latency_window=100)

    for _ in range(5_000):
        metrics.record_request(latency_ms=1.0, input_tokens=1, output_tokens=1, cost_usd=0.0)

    assert len(metrics._latencies_ms) == 100

    # The totals are counters, not samples, so they must still count everything.
    # Các tổng số là bộ đếm, không phải mẫu, nên chúng vẫn phải đếm đủ tất cả.
    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 5_000
    assert snapshot["input_tokens"] == 5_000


def test_prometheus_output_is_well_formed():
    metrics = Metrics()
    metrics.record_request(latency_ms=500, input_tokens=100, output_tokens=50, cost_usd=0.002)
    metrics.record_tool_call("tim_kiem_tai_lieu", allowed=True)

    text = metrics.render_prometheus()
    assert "agent_requests_total 1" in text
    assert 'agent_tokens_total{direction="input"} 100' in text
    assert 'agent_tool_calls_total{tool="tim_kiem_tai_lieu"} 1' in text
    assert text.endswith("\n")
