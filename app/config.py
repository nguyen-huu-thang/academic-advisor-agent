"""Application settings loaded from environment variables.

Cấu hình ứng dụng, đọc từ biến môi trường (.env).

Role in the RAG pipeline: cross-cutting config. The knobs that shape retrieval quality live
here - embedding_model and embedding_dim (step 3, must match what the chunks were indexed
with) and retrieval_top_k (step 5, how many passages the retriever returns per question).
Vai trò trong luồng RAG: cấu hình xuyên suốt. Các "núm vặn" quyết định chất lượng truy hồi
nằm ở đây - embedding_model và embedding_dim (bước 3, phải khớp với lúc đã đánh chỉ mục các
đoạn) và retrieval_top_k (bước 5, mỗi câu hỏi bộ truy hồi trả về bao nhiêu đoạn).
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Price per 1 million tokens in USD, taken from the official Gemini pricing page.
# Giá mỗi 1 triệu token (USD), lấy từ trang giá chính thức của Gemini.
PRICE_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-embedding-001": {"input": 0.15, "output": 0.0},
}


# A signing key short enough to brute-force offline is the same as no signing key: whoever
# recovers it can mint a token for any student. There is no safe default here, so the service
# refuses to start rather than fall back to one.
# Một khóa ký ngắn đến mức có thể dò vét cạn ngoại tuyến thì cũng như không có khóa ký: ai lấy
# lại được nó là cấp được token cho bất kỳ sinh viên nào. Ở đây không có giá trị mặc định nào là
# an toàn, nên dịch vụ từ chối khởi động chứ không lấy đại một giá trị.
MIN_JWT_SECRET_LENGTH = 32


def _require(name: str) -> str:
    # Read a mandatory environment variable; a missing value stops the service at startup.
    # Đọc một biến môi trường bắt buộc; thiếu giá trị thì dịch vụ dừng ngay lúc khởi động.
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Thieu bien moi truong {name}. Hay sao chep .env.example thanh .env va dien gia tri."
        )
    return value


def _require_secret(name: str) -> str:
    # Like _require, but also enforces a minimum length suitable for signing keys.
    # Như _require, nhưng đòi thêm độ dài tối thiểu phù hợp cho khóa ký.
    value = _require(name)
    if len(value) < MIN_JWT_SECRET_LENGTH:
        raise RuntimeError(
            f"Bien moi truong {name} phai dai it nhat {MIN_JWT_SECRET_LENGTH} ky tu. "
            'Sinh mot khoa moi: python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return value


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    chat_model: str
    embedding_model: str
    embedding_dim: int
    database_url: str
    max_tool_iterations: int
    retrieval_top_k: int
    current_semester: str
    # Authentication. The service both issues and verifies its own access tokens, so it needs
    # the signing key, the two claims that say a token was meant for this service, and how long
    # a token stays good for.
    # Xác thực. Dịch vụ vừa tự cấp vừa tự xác minh access token của chính nó, nên nó cần khóa ký,
    # hai claim nói lên rằng token được cấp cho đúng dịch vụ này, và thời hạn token còn hiệu lực.
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    # The access token is not stored anywhere, so it cannot be taken back before it expires.
    # Its lifetime IS the revocation delay: a student whose session is revoked keeps working for
    # at most this long. Fifteen minutes is the price paid for not touching the database on every
    # single request, and it is a price worth naming out loud rather than leaving implicit.
    # Access token không được lưu ở đâu, nên không thể rút lại trước hạn. Thời gian sống của nó
    # CHÍNH LÀ độ trễ của việc thu hồi: một sinh viên bị thu hồi phiên vẫn dùng được nhiều nhất
    # bằng khoảng thời gian này. Mười lăm phút là cái giá phải trả cho việc không chạm vào database
    # ở mỗi request, và đó là cái giá đáng được gọi tên ra thay vì để ngầm.
    access_token_ttl_minutes: int
    refresh_token_ttl_days: int
    # Whether the refresh cookie carries the Secure flag, which stops the browser from ever
    # sending it over plain HTTP. True everywhere that matters; the only reason it can be turned
    # off is that a local dev server speaks http, and a Secure cookie would simply never be sent.
    # Default is on: a footgun should have to be picked up deliberately.
    # Cookie refresh có mang cờ Secure hay không, vốn là thứ ngăn trình duyệt gửi nó qua HTTP trần.
    # Bật ở mọi nơi đáng kể; lý do duy nhất để tắt nó là máy chủ dev cục bộ chạy http, và một cookie
    # Secure thì sẽ không bao giờ được gửi đi. Mặc định là bật: một khẩu súng tự bắn chân mình thì
    # phải cố ý nhặt lên mới cầm được.
    cookie_secure: bool
    login_max_attempts: int
    login_lockout_minutes: int
    # A separate secret for the operational endpoints, deliberately not the student's token.
    # /metrics and /stats report what the service costs to run - token counts, USD spent, how
    # often the guardrail fires - which is the operator's business and nobody else's. A student
    # who can log in should not thereby be able to read the bill.
    # Một khóa bí mật riêng cho các endpoint vận hành, cố ý không dùng token của sinh viên.
    # /metrics và /stats báo cáo chi phí vận hành của dịch vụ - số token, số tiền USD đã tiêu, số
    # lần guardrail chặn - vốn là việc của người vận hành chứ không phải của ai khác. Một sinh viên
    # đăng nhập được thì không vì thế mà đọc được hóa đơn.
    metrics_token: str
    # The credit ceiling depends on how the student is doing, not on who is asking. A student
    # on academic warning is held to a lower ceiling so they can concentrate on fewer courses.
    # Trần tín chỉ phụ thuộc vào kết quả học tập của sinh viên, không phụ thuộc vào việc ai
    # đang hỏi. Sinh viên bị cảnh báo học vụ chịu trần thấp hơn để tập trung vào ít môn hơn.
    max_credits_by_status: dict[str, int]

    def max_credits_for(self, academic_status: str) -> int:
        """The credit ceiling for a student in this academic standing.

        Trần tín chỉ áp dụng cho sinh viên đang ở tình trạng học vụ này.

        An unknown status falls back to the strictest ceiling rather than the most generous
        one: if the data is not understood, the safe reading is the restrictive one.
        Một tình trạng lạ không nhận ra được thì lấy trần chặt nhất chứ không lấy trần rộng
        nhất: khi không hiểu dữ liệu, cách đọc an toàn là cách đọc hạn chế.
        """
        if academic_status in self.max_credits_by_status:
            return self.max_credits_by_status[academic_status]
        return min(self.max_credits_by_status.values())


def load_settings() -> Settings:
    return Settings(
        gemini_api_key=_require("GEMINI_API_KEY"),
        chat_model=os.getenv("CHAT_MODEL", "gemini-2.5-flash"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dim=int(os.getenv("EMBEDDING_DIM", "768")),
        database_url=_require("DATABASE_URL"),
        max_tool_iterations=int(os.getenv("MAX_TOOL_ITERATIONS", "5")),
        retrieval_top_k=int(os.getenv("RETRIEVAL_TOP_K", "4")),
        current_semester=os.getenv("CURRENT_SEMESTER", "2026.1"),
        jwt_secret=_require_secret("JWT_SECRET"),
        jwt_issuer=os.getenv("JWT_ISSUER", "academic-advisor"),
        jwt_audience=os.getenv("JWT_AUDIENCE", "academic-advisor-api"),
        access_token_ttl_minutes=int(os.getenv("ACCESS_TOKEN_TTL_MINUTES", "15")),
        refresh_token_ttl_days=int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "14")),
        cookie_secure=os.getenv("COOKIE_SECURE", "true").strip().lower() != "false",
        login_max_attempts=int(os.getenv("LOGIN_MAX_ATTEMPTS", "5")),
        login_lockout_minutes=int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15")),
        metrics_token=_require_secret("METRICS_TOKEN"),
        max_credits_by_status={
            "binh_thuong": int(os.getenv("MAX_CREDITS_BINH_THUONG", "24")),
            "canh_bao_1": int(os.getenv("MAX_CREDITS_CANH_BAO_1", "18")),
            "canh_bao_2": int(os.getenv("MAX_CREDITS_CANH_BAO_2", "14")),
        },
    )


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the USD cost of one model call.

    Ước tính chi phí (USD) của một lần gọi model.
    """
    price = PRICE_PER_1M_TOKENS.get(model)
    if price is None:
        return 0.0
    return (
        input_tokens / 1_000_000 * price["input"]
        + output_tokens / 1_000_000 * price["output"]
    )
