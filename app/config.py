"""Application settings loaded from environment variables.

Cau hinh ung dung, doc tu bien moi truong (.env).
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Price per 1 million tokens in USD, taken from the official Gemini pricing page.
# Gia moi 1 trieu token (USD), lay tu trang gia chinh thuc cua Gemini.
PRICE_PER_1M_TOKENS: dict[str, dict[str, float]] = {
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40},
    "gemini-embedding-001": {"input": 0.15, "output": 0.0},
}


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Thieu bien moi truong {name}. Hay sao chep .env.example thanh .env va dien gia tri."
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
    # The credit ceiling depends on how the student is doing, not on who is asking. A student
    # on academic warning is held to a lower ceiling so they can concentrate on fewer courses.
    # Tran tin chi phu thuoc vao ket qua hoc tap cua sinh vien, khong phu thuoc vao viec ai
    # dang hoi. Sinh vien bi canh bao hoc vu chiu tran thap hon de tap trung vao it mon hon.
    max_credits_by_status: dict[str, int]

    def max_credits_for(self, academic_status: str) -> int:
        """The credit ceiling for a student in this academic standing.

        Tran tin chi ap dung cho sinh vien dang o tinh trang hoc vu nay.

        An unknown status falls back to the strictest ceiling rather than the most generous
        one: if the data is not understood, the safe reading is the restrictive one.
        Mot tinh trang la khong nhan ra duoc thi lay tran chat nhat chu khong lay tran rong
        nhat: khi khong hieu du lieu, cach doc an toan la cach doc han che.
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
        max_credits_by_status={
            "binh_thuong": int(os.getenv("MAX_CREDITS_BINH_THUONG", "24")),
            "canh_bao_1": int(os.getenv("MAX_CREDITS_CANH_BAO_1", "18")),
            "canh_bao_2": int(os.getenv("MAX_CREDITS_CANH_BAO_2", "14")),
        },
    )


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate the USD cost of one model call.

    Uoc tinh chi phi (USD) cua mot lan goi model.
    """
    price = PRICE_PER_1M_TOKENS.get(model)
    if price is None:
        return 0.0
    return (
        input_tokens / 1_000_000 * price["input"]
        + output_tokens / 1_000_000 * price["output"]
    )
