"""Password hashing with scrypt.

Băm mật khẩu bằng scrypt.

A password table is the one place where a fast hash is a liability. SHA-256 is designed to be
quick, which is exactly the wrong property here: it lets whoever steals the table try billions
of guesses a second. scrypt is deliberately slow and memory-hard, so every guess costs the
attacker real RAM and real time, and a stolen table stays expensive to crack.
Bảng mật khẩu là đúng một chỗ mà một hàm băm nhanh lại trở thành điểm yếu. SHA-256 được thiết
kế để chạy thật nhanh, và đó đúng là tính chất sai ở đây: nó cho phép kẻ đánh cắp được bảng dữ
liệu thử hàng tỷ lần đoán mỗi giây. scrypt thì cố ý chậm và ngốn bộ nhớ, nên mỗi lần đoán đều
bắt kẻ tấn công trả giá bằng RAM thật và thời gian thật.

Nothing here reaches for the database, the clock or the network, so these rules are as cheap to
test as the guardrail is.
Không có gì ở đây đụng tới database, đồng hồ hay mạng, nên các quy tắc này dễ kiểm thử không
kém gì guardrail.
"""

import hashlib
import hmac
import os
import secrets
from functools import lru_cache

ALGORITHM = "scrypt"

# Memory used per hash is 128 * N * r bytes: 128 * 16384 * 8 = 16 MB. High enough to hurt an
# attacker running guesses in parallel, low enough that one login stays well under a tenth of a
# second on an ordinary server.
# Bộ nhớ mỗi lần băm là 128 * N * r byte: 128 * 16384 * 8 = 16 MB. Đủ lớn để làm khó kẻ tấn công
# chạy song song hàng loạt lần đoán, và đủ nhỏ để một lần đăng nhập vẫn dưới một phần mười giây
# trên một máy chủ bình thường.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

KEY_LENGTH = 32
SALT_LENGTH = 16

# Each hash carries its own random salt, so two students who happen to choose the same password
# still get two different hashes. Without it, one cracked password would unlock every account
# that shares it, and a precomputed table would crack them all at once.
# Mỗi bản băm mang một salt ngẫu nhiên riêng, nên hai sinh viên lỡ đặt trùng mật khẩu vẫn cho ra
# hai bản băm khác nhau. Nếu không có salt, một mật khẩu bị bẻ là mở được mọi tài khoản dùng
# chung mật khẩu đó, và một bảng tra cứu dựng sẵn sẽ bẻ được tất cả cùng một lúc.


def hash_password(plain: str) -> str:
    """Hash a password into a self-describing string, salt and parameters included.

    Băm mật khẩu thành một chuỗi tự mô tả, gồm cả salt lẫn các tham số.

    The parameters are stored alongside the digest rather than hard-coded into the verifier, so
    that raising them later does not invalidate every hash written before the change.
    Các tham số được lưu kèm bản băm thay vì gắn cứng vào hàm kiểm tra, để sau này có nâng tham
    số lên thì các bản băm ghi trước đó vẫn còn kiểm tra được.
    """
    salt = os.urandom(SALT_LENGTH)
    digest = _derive(plain, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LENGTH)
    return f"{ALGORITHM}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Check a password against a stored hash. A malformed hash simply fails to match.

    Kiểm tra mật khẩu với bản băm đã lưu. Một bản băm hỏng thì đơn giản là không khớp.
    """
    parsed = _parse(stored)
    if parsed is None:
        return False

    n, r, p, salt, expected = parsed
    actual = _derive(plain, salt, n, r, p, len(expected))

    # Compared in constant time. A plain `==` bails out at the first differing byte, and the
    # time it took to bail leaks how many leading bytes were right - enough, over many tries, to
    # reconstruct the digest one byte at a time.
    # So sánh trong thời gian hằng định. Phép `==` thông thường dừng lại ngay ở byte đầu tiên
    # khác nhau, và thời gian nó dừng lại lộ ra có bao nhiêu byte đầu đã đúng - qua nhiều lần
    # thử, đó là đủ để dựng lại bản băm từng byte một.
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A hash of a password nobody knows, used to keep login timing uniform.

    Bản băm của một mật khẩu không ai biết, dùng để giữ thời gian đăng nhập đều nhau.

    When the student id does not exist there is no hash to check, so a naive login would answer
    at once, while a login for a real student with a wrong password would take the full scrypt
    work. That difference is readable from the outside, and it turns the login endpoint into a
    way to ask "does this student id exist?". Verifying against this dummy makes both paths cost
    the same.
    Khi mã sinh viên không tồn tại thì không có bản băm nào để kiểm tra, nên một hàm đăng nhập
    ngây thơ sẽ trả lời ngay lập tức, trong khi một lần đăng nhập của sinh viên có thật với mật
    khẩu sai lại phải chịu trọn chi phí của scrypt. Chênh lệch đó đọc được từ bên ngoài, và nó
    biến endpoint đăng nhập thành một cách để hỏi "mã sinh viên này có tồn tại không?". Kiểm tra
    với bản băm giả này làm cả hai đường đi tốn thời gian như nhau.
    """
    return hash_password(secrets.token_hex(16))


def _derive(plain: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """Read a stored hash back into its parts, or None if it is not one of ours.

    Đọc một bản băm đã lưu về lại các thành phần, hoặc None nếu nó không phải định dạng của ta.
    """
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != ALGORITHM:
        return None

    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return None

    if not salt or not expected:
        return None
    return n, r, p, salt, expected
