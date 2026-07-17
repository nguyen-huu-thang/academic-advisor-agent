"""Tests for the authentication layer.

Kiểm thử tầng xác thực.

Like the guardrail tests, these touch no database and no network: passwords, tokens and the
lockout counter are all pure functions of their inputs, and the clock is passed in rather than
read. That is what lets an expiry be tested without waiting an hour for one.
Giống các bài test của guardrail, các bài test ở đây không đụng tới database và không đụng tới
mạng: mật khẩu, token và bộ đếm khóa tài khoản đều là hàm thuần của đầu vào, còn đồng hồ thì
được truyền vào chứ không đọc tại chỗ. Chính vì vậy mà có thể kiểm tra một token hết hạn mà
không phải ngồi đợi một tiếng đồng hồ.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_student, require_ops_token
from app.auth.passwords import hash_password, verify_password
from app.auth.refresh import hash_token as hash_refresh_token
from app.auth.throttle import LoginThrottle
from app.auth.tokens import InvalidToken, decode_access_token, issue_access_token
from app.config import Settings

SECRET = "a" * 48
OTHER_SECRET = "b" * 48
OPS_TOKEN = "c" * 48

AN = "22021001"
CUONG = "22021003"


def make_settings(**overrides) -> Settings:
    values = {
        "gemini_api_key": "khong-dung-toi",
        "chat_model": "gemini-3.1-flash-lite",
        "embedding_model": "gemini-embedding-001",
        "embedding_dim": 768,
        "database_url": "postgresql://khong-dung-toi",
        "max_tool_iterations": 5,
        "retrieval_top_k": 4,
        "current_semester": "2026.1",
        "max_credits_by_status": {"binh_thuong": 24, "canh_bao_1": 18, "canh_bao_2": 14},
        "jwt_secret": SECRET,
        "jwt_issuer": "academic-advisor",
        "jwt_audience": "academic-advisor-api",
        "access_token_ttl_minutes": 60,
        "refresh_token_ttl_days": 14,
        "cookie_secure": True,
        "login_max_attempts": 5,
        "login_lockout_minutes": 15,
        "metrics_token": OPS_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


# Mật khẩu
# Passwords


def test_hash_verifies_against_the_right_password():
    stored = hash_password("Sinhvien@2026")
    assert verify_password("Sinhvien@2026", stored)


def test_hash_rejects_the_wrong_password():
    stored = hash_password("Sinhvien@2026")
    assert not verify_password("Sinhvien@2025", stored)


def test_same_password_hashes_differently_each_time():
    """The salt is what makes this true, and it is why one cracked hash cracks only one account.

    Salt là thứ làm nên điều này, và là lý do một bản băm bị bẻ chỉ mở được đúng một tài khoản.
    """
    assert hash_password("Sinhvien@2026") != hash_password("Sinhvien@2026")


def test_empty_stored_hash_never_authenticates():
    """A student row left with the migration default must be unusable, not open to everyone.

    Một dòng sinh viên còn mang giá trị mặc định của lần đổi lược đồ phải là không dùng được,
    chứ không phải mở cho tất cả mọi người.
    """
    assert not verify_password("", "")
    assert not verify_password("bat ky mat khau nao", "")


def test_malformed_stored_hash_is_refused_not_crashed():
    assert not verify_password("x", "scrypt$khong$phai$so$zz$zz")
    assert not verify_password("x", "md5$deadbeef")


# Băm refresh token
# Hashing the refresh token


def test_refresh_token_hash_is_deterministic_so_it_can_be_looked_up():
    """No salt, deliberately: the hash IS the lookup key, and a salted hash cannot be looked up.

    Cố ý không salt: bản băm CHÍNH LÀ khóa tra cứu, mà một bản băm có salt thì không tra cứu được.

    That is safe here for a reason that does not hold for passwords. A password is short and
    guessable, so it needs a salt and a slow hash. A refresh token is 256 random bits: nobody is
    guessing it, so the hash exists only to make a stolen table useless - not to make a stolen
    token hard to find.
    Ở đây điều đó an toàn vì một lý do không đúng với mật khẩu. Mật khẩu thì ngắn và đoán được, nên
    nó cần salt và cần một hàm băm chậm. Refresh token là 256 bit ngẫu nhiên: không ai đoán nó cả,
    nên bản băm chỉ tồn tại để một cái bảng bị đánh cắp trở nên vô dụng - chứ không phải để một cái
    token bị đánh cắp trở nên khó tìm.
    """
    assert hash_refresh_token("abc") == hash_refresh_token("abc")
    assert hash_refresh_token("abc") != hash_refresh_token("abd")


def test_refresh_token_hash_does_not_contain_the_token():
    raw = "mot-refresh-token-bi-mat"

    assert raw not in hash_refresh_token(raw)


# Token


def test_token_round_trip_carries_the_student_id():
    settings = make_settings()
    token, expires_in = issue_access_token(AN, settings)

    claims = decode_access_token(token, settings)

    assert claims["sub"] == AN
    assert expires_in == 3600


def test_expired_token_is_refused():
    settings = make_settings(access_token_ttl_minutes=60)
    issued_long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    token, _ = issue_access_token(AN, settings, now=issued_long_ago)

    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_token_signed_with_another_secret_is_refused():
    token, _ = issue_access_token(AN, make_settings(jwt_secret=OTHER_SECRET))

    with pytest.raises(InvalidToken):
        decode_access_token(token, make_settings())


def test_tampering_with_the_student_id_breaks_the_signature():
    """The attack the whole layer exists to stop: swap the subject, keep the token.

    Chính là đòn tấn công mà toàn bộ tầng này sinh ra để chặn: đổi mã sinh viên, giữ nguyên token.
    """
    settings = make_settings()
    token, _ = issue_access_token(AN, settings)

    claims = jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    claims["sub"] = CUONG
    forged = jwt.encode(claims, OTHER_SECRET, algorithm="HS256")

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


def test_unsigned_token_is_refused():
    """`alg: none` says "trust me, I need no signature". Pinning the algorithm is what refuses it.

    `alg: none` nghĩa là "cứ tin tôi đi, tôi không cần chữ ký". Việc ghim thuật toán chặn điều đó.
    """
    settings = make_settings()
    forged = jwt.encode(
        {
            "sub": CUONG,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "typ": "access",
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


def test_token_meant_for_another_service_is_refused():
    """A correctly signed token is not automatically a token meant for us.

    Một token được ký đúng chưa chắc là một token dành cho ta.
    """
    token, _ = issue_access_token(AN, make_settings(jwt_audience="mot-dich-vu-khac"))

    with pytest.raises(InvalidToken):
        decode_access_token(token, make_settings())


def test_token_from_another_issuer_is_refused():
    token, _ = issue_access_token(AN, make_settings(jwt_issuer="ai-do-khac"))

    with pytest.raises(InvalidToken):
        decode_access_token(token, make_settings())


def test_token_without_an_expiry_is_refused():
    """A token with no expiry is valid forever, so its absence must itself be an error.

    Một token không có hạn dùng thì có giá trị vĩnh viễn, nên việc nó thiếu hạn dùng tự nó đã
    phải là một lỗi.
    """
    settings = make_settings()
    forged = jwt.encode(
        {
            "sub": CUONG,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": datetime.now(timezone.utc),
            "typ": "access",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


def test_a_non_access_token_cannot_be_used_as_one():
    settings = make_settings()
    forged = jwt.encode(
        {
            "sub": CUONG,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "typ": "refresh",
        },
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


# Khóa tạm thời sau nhiều lần sai
# Lockout after repeated failures


def test_throttle_locks_after_the_last_allowed_attempt():
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=900)

    for _ in range(2):
        throttle.record_failure(AN, now=0.0)
    assert throttle.seconds_until_unlocked(AN, now=0.0) is None

    throttle.record_failure(AN, now=0.0)
    assert throttle.seconds_until_unlocked(AN, now=0.0) == 900


def test_throttle_releases_the_key_once_the_lockout_has_passed():
    throttle = LoginThrottle(max_attempts=1, lockout_seconds=900)
    throttle.record_failure(AN, now=0.0)

    assert throttle.seconds_until_unlocked(AN, now=899.0) == 1
    assert throttle.seconds_until_unlocked(AN, now=900.0) is None


def test_throttle_forgets_failures_after_a_successful_login():
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=900)
    throttle.record_failure(AN, now=0.0)
    throttle.record_failure(AN, now=0.0)

    throttle.record_success(AN)
    throttle.record_failure(AN, now=0.0)

    assert throttle.seconds_until_unlocked(AN, now=0.0) is None


def test_throttle_forgets_failures_once_the_window_has_passed():
    """Five slips spread over a year is a forgetful student, not an attacker.

    Năm lần lỡ tay rải rác cả năm là một sinh viên hay quên, không phải một kẻ tấn công.

    Without a window the counter only ever climbs, so someone who mistypes their password once a
    month would be locked out on the fifth month, having done nothing wrong.
    Nếu không có cửa sổ thời gian thì bộ đếm chỉ có tăng, nên một người một tháng gõ nhầm mật khẩu
    một lần sẽ bị khóa tài khoản vào tháng thứ năm, dù không làm gì sai cả.
    """
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=900)

    for month in range(10):
        throttle.record_failure(AN, now=month * 30 * 86400.0)
        assert throttle.seconds_until_unlocked(AN, now=month * 30 * 86400.0) is None


def test_throttle_still_locks_five_quick_failures_inside_the_window():
    """The other side of the window: bunched failures must still lock.

    Mặt còn lại của cửa sổ thời gian: các lần sai dồn dập thì vẫn phải bị khóa.
    """
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=900)

    for second in range(3):
        throttle.record_failure(AN, now=float(second))

    assert throttle.seconds_until_unlocked(AN, now=3.0) is not None


def test_throttle_does_not_grow_without_bound():
    """A flood of invented student ids must not be a way to eat the service's memory.

    Một trận lụt các mã sinh viên bịa ra không được phép trở thành cách ăn mòn bộ nhớ dịch vụ.
    """
    throttle = LoginThrottle(max_attempts=5, lockout_seconds=900, max_tracked_keys=100)

    for i in range(5_000):
        throttle.record_failure(f"ma-bia-{i}", now=1.0)

    assert len(throttle._by_key) <= 100


def test_throttle_locks_one_student_without_locking_another():
    """Otherwise anyone could lock a classmate out by guessing their password wrongly on purpose.

    Nếu không, ai cũng có thể khóa tài khoản của bạn cùng lớp bằng cách cố tình nhập sai mật khẩu.
    """
    throttle = LoginThrottle(max_attempts=1, lockout_seconds=900)
    throttle.record_failure(AN, now=0.0)

    assert throttle.seconds_until_unlocked(AN, now=0.0) == 900
    assert throttle.seconds_until_unlocked(CUONG, now=0.0) is None


# Dependency: bearer token -> mã sinh viên
# The dependency that turns a bearer token into a student id


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A tiny app carrying only the dependencies, so the test needs no database and no Gemini.

    Một app nhỏ chỉ mang các dependency, nên bài test không cần database và không cần Gemini.
    """
    monkeypatch.setattr("app.auth.dependencies.load_settings", make_settings)

    app = FastAPI()

    @app.get("/whoami")
    def whoami(student_id: str = Depends(get_current_student)) -> dict:
        return {"student_id": student_id}

    @app.get("/ops", dependencies=[Depends(require_ops_token)])
    def ops() -> dict:
        return {"cost_usd": 1.23}

    return TestClient(app)


def test_request_without_a_token_is_refused(client: TestClient):
    response = client.get("/whoami")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_request_with_a_garbage_token_is_refused(client: TestClient):
    response = client.get("/whoami", headers={"Authorization": "Bearer khong-phai-token"})

    assert response.status_code == 401


def test_a_valid_token_identifies_exactly_the_student_it_names(client: TestClient):
    token, _ = issue_access_token(AN, make_settings())

    response = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["student_id"] == AN


# Endpoint vận hành
# The operational endpoints


def test_ops_endpoint_refuses_a_request_without_a_token(client: TestClient):
    assert client.get("/ops").status_code == 401


def test_ops_endpoint_refuses_a_students_access_token(client: TestClient):
    """A student who can log in must not thereby be able to read the bill.

    Một sinh viên đăng nhập được thì không vì thế mà đọc được hóa đơn.

    This is the test that says the operational endpoints are not merely "behind some auth", but
    behind a different auth: /metrics reports token counts, USD spent and how often the guardrail
    fires, and none of that is a student's business.
    Đây là bài test khẳng định các endpoint vận hành không chỉ đơn giản là "nằm sau một lớp xác
    thực nào đó", mà nằm sau MỘT lớp xác thực KHÁC: /metrics báo cáo số token, số tiền USD đã tiêu
    và số lần guardrail chặn, và không thứ nào trong số đó là việc của sinh viên.
    """
    token, _ = issue_access_token(AN, make_settings())

    response = client.get("/ops", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


def test_ops_endpoint_accepts_the_operator_token(client: TestClient):
    response = client.get("/ops", headers={"Authorization": f"Bearer {OPS_TOKEN}"})

    assert response.status_code == 200
    assert response.json()["cost_usd"] == 1.23


def test_the_body_cannot_name_a_student():
    """The regression test for the hole this whole layer was built to close.

    Bài test chặn lại đúng cái lỗ hổng mà toàn bộ tầng này được dựng lên để bịt.

    `student_id` used to be a field of ChatRequest, so a request could simply name someone else
    and read their grades. It is not a field any more, which means a `student_id` sent in the
    body today is not read, not validated, and not used - it is ignored. If anyone ever adds the
    field back, this test fails.
    `student_id` trước kia là một trường của ChatRequest, nên một request chỉ việc nêu tên người
    khác là đọc được bảng điểm của họ. Giờ nó không còn là một trường nữa, nghĩa là một
    `student_id` gửi kèm trong body hôm nay sẽ không được đọc, không được kiểm tra, và không được
    dùng - nó bị bỏ qua. Nếu sau này có ai thêm trường đó trở lại, bài test này sẽ đổ.
    """
    from app.api.routes import ChatRequest

    assert "student_id" not in ChatRequest.model_fields

    payload = ChatRequest.model_validate(
        {"session_id": "s1", "message": "cho xem bang diem", "student_id": CUONG}
    )

    assert not hasattr(payload, "student_id")
