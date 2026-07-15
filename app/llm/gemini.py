"""Thin wrapper over the Gemini API: embeddings and tool-enabled generation.

Lop bao mong quanh Gemini API: sinh embedding va sinh cau tra loi co dung tool.
"""

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import numpy as np
from google import genai
from google.genai import errors, types

from app.config import Settings, estimate_cost_usd

logger = logging.getLogger(__name__)

# Gemini caps how many texts one embed_content call may take.
# Gemini gioi han so luong van ban moi lan goi embed_content.
EMBED_BATCH_SIZE = 32

# 429 means the quota is exhausted, 503 means the model is momentarily overloaded.
# Both are worth retrying; a 400 or 404 is our own bug and retrying only wastes time.
# 429 la het quota, 503 la model dang qua tai nhat thoi. Ca hai deu dang thu lai;
# con 400 hay 404 la loi cua chinh minh, thu lai chi ton them thoi gian.
RETRYABLE_STATUS = frozenset({429, 503})
MAX_RETRIES = 2

# A customer waiting on an HTTP request will not sit through the full 51 seconds that
# Gemini sometimes asks for, so the wait is capped and the caller is told to come back.
# Khach hang dang cho mot request HTTP se khong ngoi doi tron 51 giay ma Gemini doi khi
# yeu cau, nen thoi gian cho bi gioi han va nguoi goi duoc bao quay lai sau.
MAX_RETRY_DELAY_SECONDS = 8.0

RETRY_DELAY_PATTERN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)

T = TypeVar("T")


class UpstreamUnavailable(Exception):
    """Gemini refused to serve even after retrying.

    Gemini van tu choi phuc vu sau khi da thu lai.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class Usage:
    """Token usage and estimated cost of one or more model calls.

    So token da dung va chi phi uoc tinh cua mot hoac nhieu lan goi model.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd


@dataclass
class GenerationResult:
    text: str | None
    function_calls: list[types.FunctionCall]
    content: types.Content | None
    usage: Usage


class GeminiClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _with_retry(self, description: str, call: Callable[[], T]) -> T:
        """Call Gemini, retrying the failures that are worth retrying.

        Goi Gemini, thu lai voi nhung loi dang de thu lai.
        """
        for attempt in range(MAX_RETRIES + 1):
            try:
                return call()
            except errors.APIError as error:
                status = getattr(error, "code", None)
                if status not in RETRYABLE_STATUS:
                    raise

                requested = _requested_retry_delay(error)
                if attempt == MAX_RETRIES:
                    logger.warning(
                        "%s: Gemini tra ve %s, da het so lan thu lai.", description, status
                    )
                    raise UpstreamUnavailable(
                        "Dich vu AI dang qua tai hoac het quota. Vui long thu lai sau.",
                        retry_after_seconds=requested,
                    ) from error

                delay = _backoff_delay(attempt, requested)
                logger.warning(
                    "%s: Gemini tra ve %s, cho %.1fs roi thu lai (lan %d/%d).",
                    description,
                    status,
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(delay)

        raise AssertionError("khong the toi day")

    def embed(self, texts: list[str], *, is_query: bool) -> np.ndarray:
        """Embed texts and return an L2-normalised matrix of shape (len(texts), dim).

        Sinh embedding cho danh sach van ban, tra ve ma tran da chuan hoa L2.

        Normalising here means cosine similarity later is just a dot product, and it is
        also required by Gemini when the embedding is truncated below its native size.
        Chuan hoa ngay tai day de sau nay cosine chi con la tich vo huong, va Gemini
        cung yeu cau chuan hoa khi cat ngan embedding xuong duoi kich thuoc goc.
        """
        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []

        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start : start + EMBED_BATCH_SIZE]
            response = self._with_retry(
                "embed",
                lambda batch=batch: self._client.models.embed_content(
                    model=self._settings.embedding_model,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type=task_type,
                        output_dimensionality=self._settings.embedding_dim,
                    ),
                ),
            )
            vectors.extend(embedding.values for embedding in response.embeddings)

        matrix = np.asarray(vectors, dtype=np.float64)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        # Guard against a zero vector, which would otherwise divide by zero.
        # Chan truong hop vector 0, neu khong se chia cho 0.
        norms[norms == 0] = 1.0
        return matrix / norms

    def generate(
        self,
        contents: list[types.Content],
        *,
        system_instruction: str,
        tools: list[types.Tool] | None = None,
    ) -> GenerationResult:
        """Run one turn of generation, returning any function calls the model wants.

        Chay mot luot sinh cau tra loi, tra ve cac tool ma model muon goi (neu co).
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2,
            tools=tools,
            # Disable the SDK's built-in tool loop: we run the loop ourselves so that
            # every call passes through the guardrail and the audit log.
            # Tat vong lap tool san co cua SDK: ta tu chay vong lap de moi lan goi tool
            # deu phai di qua guardrail va duoc ghi vao nhat ky kiem toan.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        response = self._with_retry(
            "generate",
            lambda: self._client.models.generate_content(
                model=self._settings.chat_model,
                contents=contents,
                config=config,
            ),
        )

        usage = self._read_usage(response)
        candidate_content = (
            response.candidates[0].content if response.candidates else None
        )

        function_calls: list[types.FunctionCall] = []
        if candidate_content and candidate_content.parts:
            function_calls = [
                part.function_call
                for part in candidate_content.parts
                if part.function_call is not None
            ]

        text = self._read_text(candidate_content)
        return GenerationResult(
            text=text,
            function_calls=function_calls,
            content=candidate_content,
            usage=usage,
        )

    def _read_usage(self, response: types.GenerateContentResponse) -> Usage:
        """Pull token counts out of the response and turn them into a Usage with a cost.

        Rut so token tu phan metadata cua phan hoi va quy ra mot Usage kem chi phi USD.

        Neu phan hoi khong kem metadata (hiem, nhung co the xay ra) thi tra ve Usage rong de
        khong lam hong phep cong don o vong lap agent.
        """
        metadata = response.usage_metadata
        if metadata is None:
            return Usage()

        input_tokens = metadata.prompt_token_count or 0
        # Thinking tokens are billed as output tokens, so they must be counted here.
        # Token suy luan duoc tinh gia nhu output token, nen phai cong vao day.
        output_tokens = (metadata.candidates_token_count or 0) + (
            metadata.thoughts_token_count or 0
        )
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost_usd(
                self._settings.chat_model, input_tokens, output_tokens
            ),
        )

    @staticmethod
    def _read_text(content: types.Content | None) -> str | None:
        """Join the text parts of a model response into one string, or None if there is no text.

        Noi cac phan text cua phan hoi thanh mot chuoi, hoac None neu khong co text nao.

        Mot phan hoi cua Gemini co the gom nhieu "part": co part la text, co part la lenh goi
        tool. Ham nay chi gom cac part text lai; khi model chi goi tool ma khong noi gi thi tra
        ve None.
        """
        if content is None or not content.parts:
            return None
        pieces = [part.text for part in content.parts if part.text]
        return "".join(pieces) if pieces else None


def _requested_retry_delay(error: errors.APIError) -> float | None:
    """Read the delay Gemini itself asked for, if it said one.

    Doc thoi gian cho ma chinh Gemini yeu cau, neu no co noi.
    """
    details = getattr(error, "details", None)
    violations = details if isinstance(details, list) else []
    for item in violations:
        if isinstance(item, dict) and item.get("@type", "").endswith("RetryInfo"):
            raw = str(item.get("retryDelay", ""))
            match = re.match(r"([\d.]+)s", raw)
            if match:
                return float(match.group(1))

    match = RETRY_DELAY_PATTERN.search(str(error))
    return float(match.group(1)) if match else None


def _backoff_delay(attempt: int, requested: float | None) -> float:
    """Exponential backoff, honouring Gemini's own hint but never waiting too long.

    Cho theo cap so nhan, ton trong goi y cua Gemini nhung khong bao gio cho qua lau.

    Jitter is added so that several requests rejected at the same instant do not all wake
    up together and hit the quota again in lockstep.
    Them nhieu ngau nhien de nhieu request cung bi tu choi mot luc khong cung thuc day
    dong loat roi lai dam vao quota mot lan nua.
    """
    base = requested if requested is not None else 2.0 ** attempt
    jitter = random.uniform(0, 0.5)
    return min(base + jitter, MAX_RETRY_DELAY_SECONDS)
