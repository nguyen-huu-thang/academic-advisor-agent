"""Password hashing with scrypt.

Bam mat khau bang scrypt.

A password table is the one place where a fast hash is a liability. SHA-256 is designed to be
quick, which is exactly the wrong property here: it lets whoever steals the table try billions
of guesses a second. scrypt is deliberately slow and memory-hard, so every guess costs the
attacker real RAM and real time, and a stolen table stays expensive to crack.
Bang mat khau la dung mot cho ma mot ham bam nhanh lai tro thanh diem yeu. SHA-256 duoc thiet
ke de chay that nhanh, va do dung la tinh chat sai o day: no cho phep ke danh cap duoc bang du
lieu thu hang ty lan doan moi giay. scrypt thi co y cham va ngon bo nho, nen moi lan doan deu
bat ke tan cong tra gia bang RAM that va thoi gian that.

Nothing here reaches for the database, the clock or the network, so these rules are as cheap to
test as the guardrail is.
Khong co gi o day dung toi database, dong ho hay mang, nen cac quy tac nay de kiem thu khong
kem gi guardrail.
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
# Bo nho moi lan bam la 128 * N * r byte: 128 * 16384 * 8 = 16 MB. Du lon de lam kho ke tan cong
# chay song song hang loat lan doan, va du nho de mot lan dang nhap van duoi mot phan muoi giay
# tren mot may chu binh thuong.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1

KEY_LENGTH = 32
SALT_LENGTH = 16

# Each hash carries its own random salt, so two students who happen to choose the same password
# still get two different hashes. Without it, one cracked password would unlock every account
# that shares it, and a precomputed table would crack them all at once.
# Moi ban bam mang mot salt ngau nhien rieng, nen hai sinh vien lo dat trung mat khau van cho ra
# hai ban bam khac nhau. Neu khong co salt, mot mat khau bi be la mo duoc moi tai khoan dung
# chung mat khau do, va mot bang tra cuu dung san se be duoc tat ca cung mot luc.


def hash_password(plain: str) -> str:
    """Hash a password into a self-describing string, salt and parameters included.

    Bam mat khau thanh mot chuoi tu mo ta, gom ca salt lan cac tham so.

    The parameters are stored alongside the digest rather than hard-coded into the verifier, so
    that raising them later does not invalidate every hash written before the change.
    Cac tham so duoc luu kem ban bam thay vi gan cung vao ham kiem tra, de sau nay co nang tham
    so len thi cac ban bam ghi truoc do van con kiem tra duoc.
    """
    salt = os.urandom(SALT_LENGTH)
    digest = _derive(plain, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, KEY_LENGTH)
    return f"{ALGORITHM}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Check a password against a stored hash. A malformed hash simply fails to match.

    Kiem tra mat khau voi ban bam da luu. Mot ban bam hong thi don gian la khong khop.
    """
    parsed = _parse(stored)
    if parsed is None:
        return False

    n, r, p, salt, expected = parsed
    actual = _derive(plain, salt, n, r, p, len(expected))

    # Compared in constant time. A plain `==` bails out at the first differing byte, and the
    # time it took to bail leaks how many leading bytes were right - enough, over many tries, to
    # reconstruct the digest one byte at a time.
    # So sanh trong thoi gian hang dinh. Phep `==` thong thuong dung lai ngay o byte dau tien
    # khac nhau, va thoi gian no dung lai lo ra co bao nhieu byte dau da dung - qua nhieu lan
    # thu, do la du de dung lai ban bam tung byte mot.
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A hash of a password nobody knows, used to keep login timing uniform.

    Ban bam cua mot mat khau khong ai biet, dung de giu thoi gian dang nhap deu nhau.

    When the student id does not exist there is no hash to check, so a naive login would answer
    at once, while a login for a real student with a wrong password would take the full scrypt
    work. That difference is readable from the outside, and it turns the login endpoint into a
    way to ask "does this student id exist?". Verifying against this dummy makes both paths cost
    the same.
    Khi ma sinh vien khong ton tai thi khong co ban bam nao de kiem tra, nen mot ham dang nhap
    ngay tho se tra loi ngay lap tuc, trong khi mot lan dang nhap cua sinh vien co that voi mat
    khau sai lai phai chiu tron chi phi cua scrypt. Chenh lech do doc duoc tu ben ngoai, va no
    bien endpoint dang nhap thanh mot cach de hoi "ma sinh vien nay co ton tai khong?". Kiem tra
    voi ban bam gia nay lam ca hai duong di ton thoi gian nhu nhau.
    """
    return hash_password(secrets.token_hex(16))


def _derive(plain: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen)


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    """Read a stored hash back into its parts, or None if it is not one of ours.

    Doc mot ban bam da luu ve lai cac thanh phan, hoac None neu no khong phai dinh dang cua ta.
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
