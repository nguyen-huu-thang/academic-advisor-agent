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

# An upper bound on how many keys are tracked at once. Without it, every student id an attacker
# invents would leave a row behind for good, and a flood of made-up ids would be a way to eat the
# service's memory rather than to guess a password.
# Chan tren cho so luong khoa duoc theo doi cung luc. Neu khong co no, moi ma sinh vien ke tan
# cong bia ra deu de lai mot dong vinh vien, va viec doi mot loat ma bia se tro thanh cach an mon
# bo nho cua dich vu chu khong con la cach do mat khau.
MAX_TRACKED_KEYS = 10_000


@dataclass
class _Attempts:
    failures: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


class LoginThrottle:
    def __init__(
        self,
        *,
        max_attempts: int,
        lockout_seconds: float,
        max_tracked_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self._max_attempts = max_attempts
        self._lockout_seconds = lockout_seconds
        self._max_tracked_keys = max_tracked_keys
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

        The count only holds within a window. The rule is "five wrong passwords within fifteen
        minutes", not "five wrong passwords ever: a student who mistypes once a month is not
        under attack, and locking them out after five slow slips would be punishing the wrong
        person entirely.
        Bo dem chi co hieu luc trong mot cua so thoi gian. Quy tac la "sai 5 lan trong vong 15
        phut", khong phai "sai 5 lan tinh tu dau: mot sinh vien mot thang go nham mot lan thi
        khong phai dang bi tan cong, va khoa tai khoan cua em sau nam lan lo tay rai rac la
        trung phat nham hoan toan.
        """
        with self._lock:
            state = self._by_key.get(key)
            if state is None or self._is_stale(state, now):
                state = _Attempts()
                self._by_key[key] = state

            state.failures += 1
            state.last_failure = now

            if state.failures >= self._max_attempts:
                state.locked_until = now + self._lockout_seconds
                state.failures = 0

            if len(self._by_key) > self._max_tracked_keys:
                self._prune(now)

    def record_success(self, key: str) -> None:
        """Forget the failures of someone who has just proved they know the password.

        Quen di cac lan sai cua nguoi vua chung minh duoc la ho biet mat khau.
        """
        with self._lock:
            self._by_key.pop(key, None)

    def _is_stale(self, state: _Attempts, now: float) -> bool:
        """A key that is neither locked nor recently failing is a key worth forgetting.

        Mot khoa khong bi khoa va cung khong sai gan day thi la mot khoa dang duoc quen di.
        """
        if state.locked_until > now:
            return False
        return now - state.last_failure > self._lockout_seconds

    def _prune(self, now: float) -> None:
        """Drop what can be forgotten, and if that is not enough, drop the quietest keys.

        Bo di nhung gi co the quen, va neu van chua du thi bo tiep cac khoa im ang nhat.

        Reaching the second half means the table is full of keys that are all either locked or
        failing right now - which is a flood, not ordinary use. Evicting under a flood can drop a
        genuinely locked key and hand its owner back their attempts, and there is no way around
        that while the counters live in one process's memory: this is exactly the point at which
        the state has to move to somewhere shared, like Redis.
        Neu phai lam toi nua sau nghia la bang dang day nhung khoa hoac dang bi khoa hoac dang sai
        ngay luc nay - do la mot tran lut, khong phai su dung binh thuong. Duoi mot tran lut, viec
        loai bot co the loai nham mot khoa dang thuc su bi khoa va tra lai luot thu cho chu cua
        no, va khong co cach nao tranh duoc dieu do chung nao bo dem con nam trong bo nho cua mot
        tien trinh: day chinh la diem ma trang thai nay phai duoc dua ra mot cho dung chung, vi du
        Redis.
        """
        for key in [k for k, state in self._by_key.items() if self._is_stale(state, now)]:
            del self._by_key[key]

        excess = len(self._by_key) - self._max_tracked_keys
        if excess <= 0:
            return

        quietest = sorted(
            self._by_key.items(),
            key=lambda item: max(item[1].last_failure, item[1].locked_until),
        )
        for key, _ in quietest[:excess]:
            del self._by_key[key]
