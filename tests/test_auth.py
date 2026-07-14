"""Tests for the authentication layer.

Kiem thu tang xac thuc.

Like the guardrail tests, these touch no database and no network: passwords, tokens and the
lockout counter are all pure functions of their inputs, and the clock is passed in rather than
read. That is what lets an expiry be tested without waiting an hour for one.
Giong cac bai test cua guardrail, cac bai test o day khong dung toi database va khong dung toi
mang: mat khau, token va bo dem khoa tai khoan deu la ham thuan cua dau vao, con dong ho thi
duoc truyen vao chu khong doc tai cho. Chinh vi vay ma co the kiem tra mot token het han ma
khong phai ngoi doi mot tieng dong ho.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_student
from app.auth.passwords import hash_password, verify_password
from app.auth.throttle import LoginThrottle
from app.auth.tokens import InvalidToken, decode_access_token, issue_access_token
from app.config import Settings

SECRET = "a" * 48
OTHER_SECRET = "b" * 48

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
        "login_max_attempts": 5,
        "login_lockout_minutes": 15,
    }
    values.update(overrides)
    return Settings(**values)


# Mat khau
# Passwords


def test_hash_verifies_against_the_right_password():
    stored = hash_password("Sinhvien@2026")
    assert verify_password("Sinhvien@2026", stored)


def test_hash_rejects_the_wrong_password():
    stored = hash_password("Sinhvien@2026")
    assert not verify_password("Sinhvien@2025", stored)


def test_same_password_hashes_differently_each_time():
    """The salt is what makes this true, and it is why one cracked hash cracks only one account.

    Salt la thu lam nen dieu nay, va la ly do mot ban bam bi be chi mo duoc dung mot tai khoan.
    """
    assert hash_password("Sinhvien@2026") != hash_password("Sinhvien@2026")


def test_empty_stored_hash_never_authenticates():
    """A student row left with the migration default must be unusable, not open to everyone.

    Mot dong sinh vien con mang gia tri mac dinh cua lan doi luoc do phai la khong dung duoc,
    chu khong phai mo cho tat ca moi nguoi.
    """
    assert not verify_password("", "")
    assert not verify_password("bat ky mat khau nao", "")


def test_malformed_stored_hash_is_refused_not_crashed():
    assert not verify_password("x", "scrypt$khong$phai$so$zz$zz")
    assert not verify_password("x", "md5$deadbeef")


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

    Chinh la don tan cong ma toan bo tang nay sinh ra de chan: doi ma sinh vien, giu nguyen token.
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

    `alg: none` nghia la "cu tin toi di, toi khong can chu ky". Viec ghim thuat toan chan dieu do.
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

    Mot token duoc ky dung chua chac la mot token danh cho ta.
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

    Mot token khong co han dung thi co gia tri vinh vien, nen viec no thieu han dung tu no da
    phai la mot loi.
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


# Khoa tam thoi sau nhieu lan sai
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


def test_throttle_locks_one_student_without_locking_another():
    """Otherwise anyone could lock a classmate out by guessing their password wrongly on purpose.

    Neu khong, ai cung co the khoa tai khoan cua ban cung lop bang cach co tinh nhap sai mat khau.
    """
    throttle = LoginThrottle(max_attempts=1, lockout_seconds=900)
    throttle.record_failure(AN, now=0.0)

    assert throttle.seconds_until_unlocked(AN, now=0.0) == 900
    assert throttle.seconds_until_unlocked(CUONG, now=0.0) is None


# Dependency: bearer token -> ma sinh vien
# The dependency that turns a bearer token into a student id


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A tiny app carrying only the dependency, so the test needs no database and no Gemini.

    Mot app nho chi mang dependency, nen bai test khong can database va khong can Gemini.
    """
    monkeypatch.setattr("app.auth.dependencies.load_settings", make_settings)

    app = FastAPI()

    @app.get("/whoami")
    def whoami(student_id: str = Depends(get_current_student)) -> dict:
        return {"student_id": student_id}

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


def test_the_body_cannot_name_a_student():
    """The regression test for the hole this whole layer was built to close.

    Bai test chan lai dung cai lo hong ma toan bo tang nay duoc dung len de bit.

    `student_id` used to be a field of ChatRequest, so a request could simply name someone else
    and read their grades. It is not a field any more, which means a `student_id` sent in the
    body today is not read, not validated, and not used - it is ignored. If anyone ever adds the
    field back, this test fails.
    `student_id` truoc kia la mot truong cua ChatRequest, nen mot request chi viec neu ten nguoi
    khac la doc duoc bang diem cua ho. Gio no khong con la mot truong nua, nghia la mot
    `student_id` gui kem trong body hom nay se khong duoc doc, khong duoc kiem tra, va khong duoc
    dung - no bi bo qua. Neu sau nay co ai them truong do tro lai, bai test nay se do.
    """
    from app.api.routes import ChatRequest

    assert "student_id" not in ChatRequest.model_fields

    payload = ChatRequest.model_validate(
        {"session_id": "s1", "message": "cho xem bang diem", "student_id": CUONG}
    )

    assert not hasattr(payload, "student_id")
