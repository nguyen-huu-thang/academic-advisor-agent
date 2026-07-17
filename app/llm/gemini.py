"""Thin wrapper over the Gemini API: embeddings and tool-enabled generation.

Lớp bao mỏng quanh Gemini API: sinh embedding và sinh câu trả lời có dùng tool.
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
# Gemini giới hạn số lượng văn bản mỗi lần gọi embed_content.
EMBED_BATCH_SIZE = 32

# 429 means the quota is exhausted, 503 means the model is momentarily overloaded.
# Both are worth retrying; a 400 or 404 is our own bug and retrying only wastes time.
# 429 là hết quota, 503 là model đang quá tải nhất thời. Cả hai đều đáng thử lại;
# còn 400 hay 404 là lỗi của chính mình, thử lại chỉ tốn thêm thời gian.
RETRYABLE_STATUS = frozenset({429, 503})
MAX_RETRIES = 2

# A customer waiting on an HTTP request will not sit through the full 51 seconds that
# Gemini sometimes asks for, so the wait is capped and the caller is told to come back.
# Khách hàng đang chờ một request HTTP sẽ không ngồi đợi trọn 51 giây mà Gemini đôi khi
# yêu cầu, nên thời gian chờ bị giới hạn và người gọi được báo quay lại sau.
MAX_RETRY_DELAY_SECONDS = 8.0

RETRY_DELAY_PATTERN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)

T = TypeVar("T")


class UpstreamUnavailable(Exception):
    """Gemini refused to serve even after retrying.

    Gemini vẫn từ chối phục vụ sau khi đã thử lại.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class Usage:
    """Token usage and estimated cost of one or more model calls.

    Số token đã dùng và chi phí ước tính của một hoặc nhiều lần gọi model.
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

        Gọi Gemini, thử lại với những lỗi đáng để thử lại.
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

        Sinh embedding cho danh sách văn bản, trả về ma trận đã chuẩn hóa L2.

        Normalising here means cosine similarity later is just a dot product, and it is
        also required by Gemini when the embedding is truncated below its native size.
        Chuẩn hóa ngay tại đây để sau này cosine chỉ còn là tích vô hướng, và Gemini
        cũng yêu cầu chuẩn hóa khi cắt ngắn embedding xuống dưới kích thước gốc.
        """
        task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        vectors: list[list[float]] = []

        # Embed in batches because Gemini caps the number of texts per call.
        # `lambda batch=batch` pins the current batch value into the closure; without it,
        # a retried call would re-read the loop variable and could embed the wrong batch.
        # Sinh embedding theo từng lô vì Gemini giới hạn số văn bản mỗi lần gọi.
        # `lambda batch=batch` chốt giá trị lô hiện tại vào closure; nếu không, một lần gọi
        # thử lại sẽ đọc lại biến vòng lặp và có thể embed nhầm lô.
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
        # Chặn trường hợp vector 0, nếu không sẽ chia cho 0.
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

        Chạy một lượt sinh câu trả lời, trả về các tool mà model muốn gọi (nếu có).
        """
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.2,
            tools=tools,
            # Disable the SDK's built-in tool loop: we run the loop ourselves so that
            # every call passes through the guardrail and the audit log.
            # Tắt vòng lặp tool sẵn có của SDK: ta tự chạy vòng lặp để mỗi lần gọi tool
            # đều phải đi qua guardrail và được ghi vào nhật ký kiểm toán.
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

        # Collect every function call the model requested in this turn; there can be several.
        # Gom mọi lệnh gọi hàm mà model yêu cầu trong lượt này; có thể có nhiều lệnh cùng lúc.
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

        Rút số token từ phần metadata của phản hồi và quy ra một Usage kèm chi phí USD.

        Nếu phản hồi không kèm metadata (hiếm, nhưng có thể xảy ra) thì trả về Usage rỗng để
        không làm hỏng phép cộng dồn ở vòng lặp agent.
        """
        metadata = response.usage_metadata
        if metadata is None:
            return Usage()

        input_tokens = metadata.prompt_token_count or 0
        # Thinking tokens are billed as output tokens, so they must be counted here.
        # Token suy luận được tính giá như output token, nên phải cộng vào đây.
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

        Nối các phần text của phản hồi thành một chuỗi, hoặc None nếu không có text nào.

        Một phản hồi của Gemini có thể gồm nhiều "part": có part là text, có part là lệnh gọi
        tool. Hàm này chỉ gom các part text lại; khi model chỉ gọi tool mà không nói gì thì trả
        về None.
        """
        if content is None or not content.parts:
            return None
        pieces = [part.text for part in content.parts if part.text]
        return "".join(pieces) if pieces else None


def _requested_retry_delay(error: errors.APIError) -> float | None:
    """Read the delay Gemini itself asked for, if it said one.

    Đọc thời gian chờ mà chính Gemini yêu cầu, nếu nó có nói.

    Tìm ở hai chỗ: trước hết trong phần details có cấu trúc (mục RetryInfo), nếu không có
    thì quét chuỗi thông báo lỗi bằng regex "retry in Ns".
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

    Chờ theo cấp số nhân, tôn trọng gợi ý của Gemini nhưng không bao giờ chờ quá lâu.

    Jitter is added so that several requests rejected at the same instant do not all wake
    up together and hit the quota again in lockstep.
    Thêm nhiễu ngẫu nhiên để nhiều request cùng bị từ chối một lúc không cùng thức dậy
    đồng loạt rồi lại đâm vào quota một lần nữa.
    """
    base = requested if requested is not None else 2.0 ** attempt
    jitter = random.uniform(0, 0.5)
    return min(base + jitter, MAX_RETRY_DELAY_SECONDS)
