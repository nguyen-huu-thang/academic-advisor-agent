"""Refresh token rotation, reuse detection, and revocation.

Xoay vong refresh token, phat hien tai su dung, va thu hoi.

These need a real PostgreSQL, and that is not an accident of implementation. Being revocable
means having state somewhere, and state is the one thing a pure function cannot have. The rest of
the auth layer - passwords, JWTs, the lockout counter - is pure and needs no database; this is
where that stops, and it stops here for a reason worth being able to say out loud.
Cac bai test nay can PostgreSQL that, va do khong phai la mot tinh co trong cach cai dat. Thu hoi
duoc nghia la phai co trang thai o dau do, ma trang thai lai dung la thu mot ham thuan khong the
co. Phan con lai cua tang xac thuc - mat khau, JWT, bo dem khoa tai khoan - deu thuan va khong can
database; den day thi dieu do dung lai, va no dung lai vi mot ly do dang duoc noi thanh loi.

Chay: pytest tests/test_refresh_token.py -v
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


# Cai duoc luu la ban bam, khong phai token
# What is stored is the hash, not the token


def test_the_token_itself_is_never_written_to_the_database(database_url, settings, students):
    """If the table leaked, the rows in it still could not be presented as tokens.

    Neu bang bi lo, cac dong trong do van khong the dem trinh ra nhu mot token.
    """
    raw = issue_for_new_login(STUDENT, settings)

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        found = conn.execute(
            "SELECT count(*) AS n FROM refresh_tokens WHERE token_hash = %s", (raw,)
        ).fetchone()

    assert found["n"] == 0
    assert _row(database_url, raw) is not None


# Xoay vong
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

    Xoay vong khong mo mot ho moi, neu khong thi mot lenh thu hoi sau nay se bo sot cac token cu.
    """
    first = issue_for_new_login(STUDENT, settings)
    second = rotate(first, settings).refresh_token
    third = rotate(second, settings).refresh_token

    family = _row(database_url, first)["family_id"]
    assert _row(database_url, second)["family_id"] == family
    assert _row(database_url, third)["family_id"] == family


def test_each_login_starts_its_own_family(database_url, settings, students):
    """Signing in on a phone must not disturb the laptop, nor be revoked along with it.

    Dang nhap tren dien thoai khong duoc lam phien may tinh, va cung khong bi thu hoi theo.
    """
    laptop = issue_for_new_login(STUDENT, settings)
    phone = issue_for_new_login(STUDENT, settings)

    assert _row(database_url, laptop)["family_id"] != _row(database_url, phone)["family_id"]

    revoke_family_of(laptop)

    assert _row(database_url, laptop)["status"] == "revoked"
    assert _row(database_url, phone)["status"] == "active"


# Phat hien tai su dung
# Reuse detection


def test_reusing_a_spent_token_revokes_the_entire_family(database_url, settings, students):
    """The heart of it. A token that was already spent turns up again, so the family dies.

    Trai tim cua ca thiet ke. Mot token da tieu roi lai xuat hien, nen ca ho phai chet.

    Either a thief copied that token and is spending it behind the student's back, or the student
    is retrying a request whose reply never arrived - and from here there is no way to tell the
    two apart. So assume the worse one: if it was a thief, the family must die or the thief keeps
    the session. If it was an honest retry, the student is logged out and signs in again, which is
    an annoyance. An annoyance is recoverable; a live session in someone else's hands is not.
    Hoac mot ke trom da sao chep token do va dang tieu no sau lung sinh vien, hoac sinh vien dang
    gui lai mot request ma cau tra loi khong bao gio toi noi - va tu day khong co cach nao phan
    biet duoc. Nen cu gia dinh truong hop xau hon: neu la ke trom, ca ho phai chet, khong thi ke
    trom giu duoc phien. Neu la mot lan gui lai ngay tinh, sinh vien bi dang xuat va dang nhap lai,
    do la mot su phien toai. Phien toai thi khac phuc duoc; mot phien dang song trong tay nguoi
    khac thi khong.
    """
    stolen = issue_for_new_login(STUDENT, settings)
    current = rotate(stolen, settings).refresh_token

    # The thief spends the copy they took before the rotation.
    # Ke trom tieu ban sao ma ho da lay truoc luc xoay vong.
    with pytest.raises(RefreshTokenReused):
        rotate(stolen, settings)

    # And the token the student is legitimately holding is dead too. That is the point: the
    # service cannot tell which of the two is the thief, so it refuses to keep serving either.
    # Va token ma sinh vien dang cam mot cach chinh dang cung chet theo. Do chinh la muc dich: dich
    # vu khong biet ai trong hai nguoi la ke trom, nen no tu choi phuc vu tiep ca hai.
    assert _row(database_url, current)["status"] == "revoked"

    with pytest.raises(InvalidRefreshToken):
        rotate(current, settings)


def test_reuse_of_one_family_does_not_touch_another(database_url, settings, students):
    """Revocation must stop at the family boundary, or one bad tab logs you out everywhere.

    Viec thu hoi phai dung lai o ranh gioi cua ho, neu khong thi mot tab hong se dang xuat het.
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

    Mot token chua ai tung thay la nhieu. Mot token da bi tieu la bao dong. Hai chuyen khac nhau.
    """
    with pytest.raises(InvalidRefreshToken) as caught:
        rotate("khong-phai-token-cua-ai-ca", settings)

    assert not isinstance(caught.value, RefreshTokenReused)


# Dang xuat
# Logout


def test_logout_revokes_the_family_not_just_the_token_in_hand(database_url, settings, students):
    """Killing only the token presented would leave its parent alive, and a thief holding the
    parent could carry on refreshing as if nothing had happened.

    Neu chi giet token duoc trinh ra thi token cha van song, va mot ke trom dang giu token cha do
    van cu the ma refresh tiep nhu chua he co chuyen gi.
    """
    first = issue_for_new_login(STUDENT, settings)
    second = rotate(first, settings).refresh_token

    revoke_family_of(second)

    assert _row(database_url, first)["status"] == "revoked"
    assert _row(database_url, second)["status"] == "revoked"

    with pytest.raises(InvalidRefreshToken):
        rotate(second, settings)


# Tranh chap dong thoi
# Concurrency


def test_two_refreshes_racing_with_one_token_cannot_both_win(database_url, settings, students):
    """Twenty threads present the same token at the same instant. Exactly one may be served.

    Hai muoi luong cung trinh ra mot token trong cung mot khoanh khac. Dung mot luong duoc phuc vu.

    Without the claim being done by the UPDATE itself - the same trick the registration slip uses
    - two threads could both read the row as active and both mint a successor, leaving two live
    refresh tokens where the design allows exactly one.
    Neu viec gianh token khong duoc thuc hien bang chinh cau UPDATE - dung meo ma phieu dang ky
    dang dung - thi hai luong deu co the doc thay dong o trang thai active va deu sinh ra mot token
    ke tiep, de lai hai refresh token con song trong khi thiet ke chi cho phep dung mot.
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
    # Moi luong thua deu phai quay ve qua mot duong da thiet ke, khong phai qua mot loi database tho.
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
    # Va he qua trung thuc, noi ra chu khong giau: cac luong thua trong y het mot lan dung lai duoi
    # con mat cua dich vu, boi tu ben trong dich vu thi chung dung la nhu vay. Ca ho bi thu hoi, va
    # luong duy nhat "thang" dang cam mot token da chet.
    #
    # Day la cai gia cua viec phat hien tai su dung mot cach nghiem ngat, va do la cai gia dung nen
    # tra. Mot client ban hai muoi lenh refresh cung luc la mot client hong; mot client gui lai mot
    # lan sau khi mat cau tra loi thi bi dang xuat va dang nhap lai. Ca hai deu khong phai la mot vu
    # xam nhap. Con de lot mot token bi dung lai thi moi la.
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        alive = conn.execute(
            "SELECT count(*) AS n FROM refresh_tokens WHERE student_id = %s AND status = 'active'",
            (STUDENT,),
        ).fetchone()

    assert alive["n"] == 0
