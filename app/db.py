"""PostgreSQL connection pool.

Quản lý kết nối tới PostgreSQL bằng connection pool.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import load_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use.

    Trả về connection pool dùng chung, khởi tạo ở lần gọi đầu tiên.
    """
    global _pool
    if _pool is None:
        settings = load_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=8,
            # dict_row: every query returns rows as dicts (row["column"]) instead of tuples.
            # dict_row: mọi truy vấn trả về dòng dạng dict (row["column"]) thay vì tuple.
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator:
    """Borrow a connection from the pool for the duration of a `with` block.

    Mượn một kết nối từ pool trong suốt một khối `with`, trả lại pool khi ra khỏi khối.

    Dùng như context manager: `with get_connection() as conn:`. Khi ra khỏi khối `with`,
    kết nối không bị đóng mà được TRẢ VỀ pool để lần sau dùng lại (mở kết nối mới rất tốn
    kém). psycopg cũng tự động commit nếu khối kết thúc bình thường, hoặc rollback nếu có lỗi.
    """
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the whole pool. Call once at shutdown, not per request.

    Đóng toàn bộ pool. Chỉ gọi một lần lúc tắt dịch vụ (xem lifespan trong main.py), không
    gọi sau mỗi request. Đặt _pool về None để lần get_pool() sau sẽ dựng lên một pool mới.
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
