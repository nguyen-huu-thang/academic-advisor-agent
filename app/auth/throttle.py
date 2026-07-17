"""Lockout after repeated failed logins.

Khóa tạm thời sau nhiều lần đăng nhập sai liên tiếp.

A password check that can be retried without limit is a password check an attacker can simply
run until it succeeds. scrypt makes each guess expensive, but expensive is not impossible, and a
student's password is likely to be short. Counting failures and locking the account for a while
turns "keep guessing" from a slow attack into no attack at all.
Một phép kiểm tra mật khẩu có thể thử lại vô hạn là một phép kiểm tra mà kẻ tấn công chỉ việc
chạy mãi cho tới khi trúng. scrypt làm mỗi lần đoán trở nên đắt đỏ, nhưng đắt không có nghĩa là
bất khả thi, và mật khẩu của sinh viên thì thường ngắn. Đếm số lần sai rồi khóa tài khoản một
lúc biến "cứ đoán tiếp" từ một đòn tấn công chậm thành không còn là đòn tấn công nữa.

The clock is passed in rather than read here, which keeps this module pure and lets the tests
move time forward without sleeping.
Đồng hồ được truyền vào chứ không đọc tại chỗ, nhờ vậy module này vẫn thuần khiết và các bài
test có thể tua thời gian đi tới mà không cần ngồi chờ.

Known limit, stated rather than hidden: the counters live in this process. Two replicas behind a
load balancer keep two separate counts, so an attacker spreading guesses across them gets twice
the attempts. At this scale that is the right trade against dragging in Redis; if the service
were ever run as more than one instance, this state is the first thing that has to move out.
Giới hạn đã biết, nói ra chứ không giấu: bộ đếm nằm trong tiến trình này. Hai bản sao đứng sau
một bộ cân bằng tải sẽ giữ hai bộ đếm riêng, nên kẻ tấn công rải đều các lần đoán qua cả hai sẽ
được gấp đôi số lần thử. Ở quy mô này thì đó là đánh đổi đúng, so với việc kéo thêm Redis vào;
nhưng nếu dịch vụ được chạy nhiều hơn một bản sao, đây là thứ đầu tiên phải được đưa ra ngoài.
"""

import threading
from dataclasses import dataclass

# An upper bound on how many keys are tracked at once. Without it, every student id an attacker
# invents would leave a row behind for good, and a flood of made-up ids would be a way to eat the
# service's memory rather than to guess a password.
# Chặn trên cho số lượng khóa được theo dõi cùng lúc. Nếu không có nó, mỗi mã sinh viên kẻ tấn
# công bịa ra đều để lại một dòng vĩnh viễn, và việc dội một loạt mã bịa sẽ trở thành cách ăn mòn
# bộ nhớ của dịch vụ chứ không còn là cách dò mật khẩu.
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

        Khóa này còn phải chờ bao lâu, hoặc None nếu được phép thử ngay bây giờ.
        """
        with self._lock:
            state = self._by_key.get(key)
            if state is None or state.locked_until <= now:
                return None
            return state.locked_until - now

    def record_failure(self, key: str, now: float) -> None:
        """Count one wrong password, and lock the key once it has run out of tries.

        Ghi nhận một lần sai mật khẩu, và khóa lại khi khóa này hết lượt thử.

        The count only holds within a window. The rule is "five wrong passwords within fifteen
        minutes", not "five wrong passwords ever: a student who mistypes once a month is not
        under attack, and locking them out after five slow slips would be punishing the wrong
        person entirely.
        Bộ đếm chỉ có hiệu lực trong một cửa sổ thời gian. Quy tắc là "sai 5 lần trong vòng 15
        phút", không phải "sai 5 lần tính từ đầu: một sinh viên một tháng gõ nhầm một lần thì
        không phải đang bị tấn công, và khóa tài khoản của em sau năm lần lỡ tay rải rác là
        trừng phạt nhầm hoàn toàn.
        """
        with self._lock:
            state = self._by_key.get(key)
            if state is None or self._is_stale(state, now):
                state = _Attempts()
                self._by_key[key] = state

            state.failures += 1
            state.last_failure = now

            # Out of tries: start the lockout and reset the counter so the next window starts
            # clean once the lock expires.
            # Hết lượt thử: bắt đầu khoảng khóa và đặt lại bộ đếm để cửa sổ tiếp theo bắt đầu
            # sạch sau khi hết khóa.
            if state.failures >= self._max_attempts:
                state.locked_until = now + self._lockout_seconds
                state.failures = 0

            if len(self._by_key) > self._max_tracked_keys:
                self._prune(now)

    def record_success(self, key: str) -> None:
        """Forget the failures of someone who has just proved they know the password.

        Quên đi các lần sai của người vừa chứng minh được là họ biết mật khẩu.
        """
        with self._lock:
            self._by_key.pop(key, None)

    def _is_stale(self, state: _Attempts, now: float) -> bool:
        """A key that is neither locked nor recently failing is a key worth forgetting.

        Một khóa không bị khóa và cũng không sai gần đây thì là một khóa đáng được quên đi.
        """
        if state.locked_until > now:
            return False
        return now - state.last_failure > self._lockout_seconds

    def _prune(self, now: float) -> None:
        """Drop what can be forgotten, and if that is not enough, drop the quietest keys.

        Bỏ đi những gì có thể quên, và nếu vẫn chưa đủ thì bỏ tiếp các khóa im ắng nhất.

        Reaching the second half means the table is full of keys that are all either locked or
        failing right now - which is a flood, not ordinary use. Evicting under a flood can drop a
        genuinely locked key and hand its owner back their attempts, and there is no way around
        that while the counters live in one process's memory: this is exactly the point at which
        the state has to move to somewhere shared, like Redis.
        Nếu phải làm tới nửa sau nghĩa là bảng đang đầy những khóa hoặc đang bị khóa hoặc đang sai
        ngay lúc này - đó là một trận lụt, không phải sử dụng bình thường. Dưới một trận lụt, việc
        loại bớt có thể loại nhầm một khóa đang thực sự bị khóa và trả lại lượt thử cho chủ của
        nó, và không có cách nào tránh được điều đó chừng nào bộ đếm còn nằm trong bộ nhớ của một
        tiến trình: đây chính là điểm mà trạng thái này phải được đưa ra một chỗ dùng chung, ví dụ
        Redis.
        """
        for key in [k for k, state in self._by_key.items() if self._is_stale(state, now)]:
            del self._by_key[key]

        excess = len(self._by_key) - self._max_tracked_keys
        if excess <= 0:
            return

        # Sort by the most recent activity (last failure or lock expiry) and evict the oldest.
        # Sắp xếp theo hoạt động gần nhất (lần sai cuối hoặc hạn khóa) và loại các khóa cũ nhất.
        quietest = sorted(
            self._by_key.items(),
            key=lambda item: max(item[1].last_failure, item[1].locked_until),
        )
        for key, _ in quietest[:excess]:
            del self._by_key[key]
