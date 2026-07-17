"""Refresh token rotation, reuse detection, and revocation.

Xoay vòng refresh token, phát hiện tái sử dụng, và thu hồi.

These need a real PostgreSQL, and that is not an accident of implementation. Being revocable
means having state somewhere, and state is the one thing a pure function cannot have. The rest of
the auth layer - passwords, JWTs, the lockout counter - is pure and needs no database; this is
where that stops, and it stops here for a reason worth being able to say out loud.
Các bài test này cần PostgreSQL thật, và đó không phải là một tình cờ trong cách cài đặt. Thu hồi
được nghĩa là phải có trạng thái ở đâu đó, mà trạng thái lại đúng là thứ một hàm thuần không thể
có. Phần còn lại của tầng xác thực - mật khẩu, JWT, bộ đếm khóa tài khoản - đều thuần và không cần
database; đến đây thì điều đó dừng lại, và nó dừng lại vì một lý do đáng được nói thành lời.

Chạy: pytest tests/test_refresh_token.py -v
"""

import threading

import psycopg
import pytest
from psycopg.rows import dict_row

from app.auth.refresh import (
    InvalidRefreshToken,
    RefreshTokenReused,
    hash_token,
    issue_for_new_login,
    revoke_family_of,
    rotate,
)
from app.config import Settings, load_settings

pytestmark = pytest.mark.integration

STUDENT = "TESTRT001"
OTHER_STUDENT = "TESTRT002"


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def database_url(settings: Settings) -> str:
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3):
            pass
    except psycopg.OperationalError as error:
        pytest.skip(f"Khong ket noi duoc PostgreSQL: {error}")
    return settings.database_url


@pytest.fixture
def students(database_url: str):
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)
        for student_id in (STUDENT, OTHER_STUDENT):
            conn.execute(
                """
                INSERT INTO students (student_id, full_name, major, cohort)
                VALUES (%s, 'Sinh vien kiem thu', 'Kiem thu', 'KTEST')
                """,
                (student_id,),
            )

    yield

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM refresh_tokens WHERE student_id LIKE 'TESTRT%'")
    conn.execute("DELETE FROM students WHERE student_id LIKE 'TESTRT%'")


def _row(database_url: str, raw_token: str) -> dict | None:
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT status, family_id FROM refresh_tokens WHERE token_hash = %s",
            (hash_token(raw_token),),
        ).fetchone()


# Cái được lưu là bản băm, không phải token
# What is stored is the hash, not the token


def test_the_token_itself_is_never_written_to_the_database(database_url, settings, students):
    """If the table leaked, the rows in it still could not be presented as tokens.

    Nếu bảng bị lộ, các dòng trong đó vẫn không thể đem trình ra như một token.
    """
    raw = issue_for_new_login(STUDENT, settings)

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        found = conn.execute(
            "SELECT count(*) AS n FROM refresh_tokens WHERE token_hash = %s", (raw,)
        ).fetchone()

    assert found["n"] == 0
    assert _row(database_url, raw) is not None


# Xoay vòng
# Rotation


def test_rotation_returns_a_new_token_and_kills_the_old_one(database_url, settings, students):
    first = issue_for_new_login(STUDENT, settings)

    rotated = rotate(first, settings)

    assert rotated.student_id == STUDENT
    assert rotated.refresh_token != first
    assert _row(database_url, first)["status"] == "rotated"
    assert _row(database_url, rotated.refresh_token)["status"] == "active"


def test_the_whole_chain_stays_in_one_family(database_url, settings, students):
    """Rotation does not start a new family, or a revocation later would miss the earlier tokens.

    Xoay vòng không mở một họ mới, nếu không thì một lệnh thu hồi sau này sẽ bỏ sót các token cũ.
    """
    first = issue_for_new_login(STUDENT, settings)
    second = rotate(first, settings).refresh_token
    third = rotate(second, settings).refresh_token

    family = _row(database_url, first)["family_id"]
    assert _row(database_url, second)["family_id"] == family
    assert _row(database_url, third)["family_id"] == family


def test_each_login_starts_its_own_family(database_url, settings, students):
    """Signing in on a phone must not disturb the laptop, nor be revoked along with it.

    Đăng nhập trên điện thoại không được làm phiền máy tính, và cũng không bị thu hồi theo.
    """
    laptop = issue_for_new_login(STUDENT, settings)
    phone = issue_for_new_login(STUDENT, settings)

    assert _row(database_url, laptop)["family_id"] != _row(database_url, phone)["family_id"]

    revoke_family_of(laptop)

    assert _row(database_url, laptop)["status"] == "revoked"
    assert _row(database_url, phone)["status"] == "active"


# Phát hiện tái sử dụng
# Reuse detection


def test_reusing_a_spent_token_revokes_the_entire_family(database_url, settings, students):
    """The heart of it. A token that was already spent turns up again, so the family dies.

    Trái tim của cả thiết kế. Một token đã tiêu rồi lại xuất hiện, nên cả họ phải chết.

    Either a thief copied that token and is spending it behind the student's back, or the student
    is retrying a request whose reply never arrived - and from here there is no way to tell the
    two apart. So assume the worse one: if it was a thief, the family must die or the thief keeps
    the session. If it was an honest retry, the student is logged out and signs in again, which is
    an annoyance. An annoyance is recoverable; a live session in someone else's hands is not.
    Hoặc một kẻ trộm đã sao chép token đó và đang tiêu nó sau lưng sinh viên, hoặc sinh viên đang
    gửi lại một request mà câu trả lời không bao giờ tới nơi - và từ đây không có cách nào phân
    biệt được. Nên cứ giả định trường hợp xấu hơn: nếu là kẻ trộm, cả họ phải chết, không thì kẻ
    trộm giữ được phiên. Nếu là một lần gửi lại ngay tình, sinh viên bị đăng xuất và đăng nhập lại,
    đó là một sự phiền toái. Phiền toái thì khắc phục được; một phiên đang sống trong tay người
    khác thì không.
    """
    stolen = issue_for_new_login(STUDENT, settings)
    current = rotate(stolen, settings).refresh_token

    # The thief spends the copy they took before the rotation.
    # Kẻ trộm tiêu bản sao mà họ đã lấy trước lúc xoay vòng.
    with pytest.raises(RefreshTokenReused):
        rotate(stolen, settings)

    # And the token the student is legitimately holding is dead too. That is the point: the
    # service cannot tell which of the two is the thief, so it refuses to keep serving either.
    # Và token mà sinh viên đang cầm một cách chính đáng cũng chết theo. Đó chính là mục đích: dịch
    # vụ không biết ai trong hai người là kẻ trộm, nên nó từ chối phục vụ tiếp cả hai.
    assert _row(database_url, current)["status"] == "revoked"

    with pytest.raises(InvalidRefreshToken):
        rotate(current, settings)


def test_reuse_of_one_family_does_not_touch_another(database_url, settings, students):
    """Revocation must stop at the family boundary, or one bad tab logs you out everywhere.

    Việc thu hồi phải dừng lại ở ranh giới của họ, nếu không thì một tab hỏng sẽ đăng xuất hết.
    """
    compromised = issue_for_new_login(STUDENT, settings)
    rotate(compromised, settings)

    innocent = issue_for_new_login(STUDENT, settings)
    other_person = issue_for_new_login(OTHER_STUDENT, settings)

    with pytest.raises(RefreshTokenReused):
        rotate(compromised, settings)

    assert _row(database_url, innocent)["status"] == "active"
    assert _row(database_url, other_person)["status"] == "active"


def test_an_unknown_token_is_refused_but_raises_no_alarm(settings, students):
    """A token nobody has ever seen is noise. A token that was spent is an alarm. Different things.

    Một token chưa ai từng thấy là nhiễu. Một token đã bị tiêu là báo động. Hai chuyện khác nhau.
    """
    with pytest.raises(InvalidRefreshToken) as caught:
        rotate("khong-phai-token-cua-ai-ca", settings)

    assert not isinstance(caught.value, RefreshTokenReused)


# Đăng xuất
# Logout


def test_logout_revokes_the_family_not_just_the_token_in_hand(database_url, settings, students):
    """Killing only the token presented would leave its parent alive, and a thief holding the
    parent could carry on refreshing as if nothing had happened.

    Nếu chỉ giết token được trình ra thì token cha vẫn sống, và một kẻ trộm đang giữ token cha đó
    vẫn cứ thế mà refresh tiếp như chưa hề có chuyện gì.
    """
    first = issue_for_new_login(STUDENT, settings)
    second = rotate(first, settings).refresh_token

    revoke_family_of(second)

    assert _row(database_url, first)["status"] == "revoked"
    assert _row(database_url, second)["status"] == "revoked"

    with pytest.raises(InvalidRefreshToken):
        rotate(second, settings)


# Tranh chấp đồng thời
# Concurrency


def test_two_refreshes_racing_with_one_token_cannot_both_win(database_url, settings, students):
    """Twenty threads present the same token at the same instant. Exactly one may be served.

    Hai mươi luồng cùng trình ra một token trong cùng một khoảnh khắc. Đúng một luồng được phục vụ.

    Without the claim being done by the UPDATE itself - the same trick the registration slip uses
    - two threads could both read the row as active and both mint a successor, leaving two live
    refresh tokens where the design allows exactly one.
    Nếu việc giành token không được thực hiện bằng chính câu UPDATE - đúng mẹo mà phiếu đăng ký
    đang dùng - thì hai luồng đều có thể đọc thấy dòng ở trạng thái active và đều sinh ra một token
    kế tiếp, để lại hai refresh token còn sống trong khi thiết kế chỉ cho phép đúng một.
    """
    contenders = 20
    token = issue_for_new_login(STUDENT, settings)

    barrier = threading.Barrier(contenders)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            rotate(token, settings)
            outcome = "thanh_cong"
        except RefreshTokenReused:
            outcome = "phat_hien_dung_lai"
        except InvalidRefreshToken:
            outcome = "bi_tu_choi"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(contenders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(outcomes) == contenders, (
        f"Chi {len(outcomes)}/{contenders} luong tra ve mot ket cuc co kiem soat."
    )
    assert outcomes.count("thanh_cong") == 1, (
        f"Dung mot luong duoc phuc vu, nhung co {outcomes.count('thanh_cong')}."
    )

    # Every loser must come back through a designed path, not through a raw database error.
    # Mọi luồng thua đều phải quay về qua một đường đã thiết kế, không phải qua một lỗi database thô.
    assert outcomes.count("thanh_cong") + outcomes.count("phat_hien_dung_lai") + outcomes.count(
        "bi_tu_choi"
    ) == contenders

    # And the honest consequence, stated rather than hidden: the losers looked exactly like a
    # replay to the service, because from inside the service that is what they are. The family is
    # revoked, and the one thread that "won" is holding a token that is already dead.
    #
    # This is the cost of strict reuse detection, and it is the right cost to pay. A client that
    # fires twenty concurrent refreshes is broken; a client that retries once after a dropped
    # reply gets logged out and signs back in. Neither is a breach. Letting a replayed token
    # through would be.
    #
    # Và hệ quả trung thực, nói ra chứ không giấu: các luồng thua trông y hệt một lần dùng lại dưới
    # con mắt của dịch vụ, bởi từ bên trong dịch vụ thì chúng đúng là như vậy. Cả họ bị thu hồi, và
    # luồng duy nhất "thắng" đang cầm một token đã chết.
    #
    # Đây là cái giá của việc phát hiện tái sử dụng một cách nghiêm ngặt, và đó là cái giá đúng nên
    # trả. Một client bắn hai mươi lệnh refresh cùng lúc là một client hỏng; một client gửi lại một
    # lần sau khi mất câu trả lời thì bị đăng xuất và đăng nhập lại. Cả hai đều không phải là một vụ
    # xâm nhập. Còn để lọt một token bị dùng lại thì mới là.
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        alive = conn.execute(
            "SELECT count(*) AS n FROM refresh_tokens WHERE student_id = %s AND status = 'active'",
            (STUDENT,),
        ).fetchone()

    assert alive["n"] == 0
