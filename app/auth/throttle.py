"""Lockout after repeated failed logins.

Khoa tam thoi sau nhieu lan dang nhap sai lien tiep.

A password check that can be retried without limit is a password check an attacker can simply
run until it succeeds. scrypt makes each guess expensive, but expensive is not impossible, and a
student's password is likely to be short. Counting failures and locking the account for a while
turns "keep guessing" from a slow attack into no attack at all.
Mot phep kiem tra mat khau co the thu lai vo han la mot phep kiem tra ma ke tan cong chi viec
chay mai cho toi khi trung. scrypt lam moi lan doan tro nen dat do, nhung dat khong co nghia la
bat kha thi, va mat khau cua sinh vien thi thuong ngan. Dem so lan sai roi khoa tai khoan mot
luc bien "cu doan tiep" tu mot don tan cong cham thanh khong con la don tan cong nua.

The clock is passed in rather than read here, which keeps this module pure and lets the tests
move time forward without sleeping.
Dong ho duoc truyen vao chu khong doc tai cho, nho vay module nay van thuan khiet va cac bai
test co the tua thoi gian di toi ma khong can ngoi cho.

Known limit, stated rather than hidden: the counters live in this process. Two replicas behind a
load balancer keep two separate counts, so an attacker spreading guesses across them gets twice
the attempts. At this scale that is the right trade against dragging in Redis; if the service
were ever run as more than one instance, this state is the first thing that has to move out.
Gioi han da biet, noi ra chu khong giau: bo dem nam trong tien trinh nay. Hai ban sao dung sau
mot bo can bang tai se giu hai bo dem rieng, nen ke tan cong rai deu cac lan doan qua ca hai se
duoc gap doi so lan thu. O quy mo nay thi do la danh doi dung, so voi viec keo them Redis vao;
nhung neu dich vu duoc chay nhieu hon mot ban sao, day la thu dau tien phai duoc dua ra ngoai.
"""

import threading
from dataclasses import dataclass


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0


class LoginThrottle:
    def __init__(self, *, max_attempts: int, lockout_seconds: float) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._lock = threading.Lock()
        self._by_key: dict[str, _Attempts] = {}

    def seconds_until_unlocked(self, key: str, now: float) -> float | None:
        """How long this key must wait, or None if it may try right now.

        Khoa nay con phai cho bao lau, hoac None neu duoc phep thu ngay bay gio.
        """
        with self._lock:
            state = self._by_key.get(key)
            if state is None or state.locked_until <= now:
                return None
            return state.locked_until - now

    def record_failure(self, key: str, now: float) -> None:
        """Count one wrong password, and lock the key once it has run out of tries.

        Ghi nhan mot lan sai mat khau, va khoa lai khi khoa nay het luot thu.
        """
        with self._lock:
            state = self._by_key.setdefault(key, _Attempts())
            state.failures += 1
            if state.failures >= self._max_attempts:
                state.locked_until = now + self._lockout_seconds
                state.failures = 0

    def record_success(self, key: str) -> None:
        """Forget the failures of someone who has just proved they know the password.

        Quen di cac lan sai cua nguoi vua chung minh duoc la ho biet mat khau.
        """
        with self._lock:
            self._by_key.pop(key, None)
