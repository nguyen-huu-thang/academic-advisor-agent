"""PostgreSQL connection pool.

Quan ly ket noi toi PostgreSQL bang connection pool.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import load_settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first use.

    Tra ve connection pool dung chung, khoi tao o lan goi dau tien.
    """
    global _pool
    if _pool is None:
        settings = load_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def get_connection() -> Iterator:
    """Borrow a connection from the pool for the duration of a `with` block.

    Muon mot ket noi tu pool trong suot mot khoi `with`, tra lai pool khi ra khoi khoi.

    Dung nhu context manager: `with get_connection() as conn:`. Khi ra khoi khoi `with`,
    ket noi khong bi dong ma duoc TRA VE pool de lan sau dung lai (mo ket noi moi rat ton
    kem). psycopg cung tu dong commit neu khoi ket thuc binh thuong, hoac rollback neu co loi.
    """
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the whole pool. Call once at shutdown, not per request.

    Dong toan bo pool. Chi goi mot lan luc tat dich vu (xem lifespan trong main.py), khong
    goi sau moi request. Dat _pool ve None de lan get_pool() sau se dung len mot pool moi.
    """
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
