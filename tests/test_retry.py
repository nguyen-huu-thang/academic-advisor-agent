"""Tests for how the service reacts when Gemini rate limits it.

Kiểm thử cách dịch vụ phản ứng khi bị Gemini chặn vì quá hạn mức.
"""

from app.llm.gemini import MAX_RETRY_DELAY_SECONDS, _backoff_delay, _requested_retry_delay


class FakeApiError(Exception):
    """Stands in for google.genai.errors.APIError, which is awkward to construct.

    Đóng thế cho google.genai.errors.APIError, vì lớp thật rất khó tạo ra.
    """

    def __init__(self, message: str, details: list | None = None) -> None:
        super().__init__(message)
        self.details = details


def test_reads_the_delay_gemini_asked_for():
    error = FakeApiError(
        "429 RESOURCE_EXHAUSTED",
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "51s"},
        ],
    )
    assert _requested_retry_delay(error) == 51.0


def test_falls_back_to_the_delay_in_the_message():
    error = FakeApiError("Quota exceeded. Please retry in 12.5s.")
    assert _requested_retry_delay(error) == 12.5


def test_returns_none_when_no_delay_is_given():
    assert _requested_retry_delay(FakeApiError("503 UNAVAILABLE")) is None


def test_backoff_grows_with_each_attempt():
    first = _backoff_delay(0, None)
    second = _backoff_delay(1, None)
    assert first < second


def test_wait_is_capped_so_a_customer_is_never_left_hanging():
    """Gemini sometimes asks for 51 seconds; an HTTP caller must not wait that long.

    Gemini đôi khi yêu cầu chờ 51 giây; một người gọi qua HTTP không thể đợi lâu thế.
    """
    assert _backoff_delay(0, requested=51.0) == MAX_RETRY_DELAY_SECONDS


def test_jitter_keeps_delays_from_being_identical():
    delays = {_backoff_delay(1, None) for _ in range(20)}
    assert len(delays) > 1
