"""What happens when several students go for the last seat at the same instant.

Chuyen gi xay ra khi nhieu sinh vien cung lao vao cho ngoi cuoi cung trong cung mot khoanh khac.

The guardrail reads the seat count outside any transaction, a moment before the model is even
asked what to do. By the time a confirmation actually runs, that number may already be stale.
So the seat is counted a second time, under a row lock, inside the transaction that writes the
enrolment - and it is that second count which decides.
Guardrail doc si so ben ngoai moi transaction, tu truoc khi model duoc hoi phai lam gi. Den luc
mot lenh xac nhan thuc su chay, con so do co the da cu. Vi vay cho ngoi duoc dem lai lan thu hai,
duoi mot khoa dong, ben trong chinh transaction ghi ban dang ky - va lan dem thu hai do moi la
lan quyet dinh.

What the lock is for was measured, not assumed. Running this same scenario against a copy of
execute_registration with FOR UPDATE taken out still ends with 1 student in a 1-seat class: the
CHECK constraint holds the line. But the other 19 fail with a CheckViolation from PostgreSQL
instead of a RegistrationRejected, which in the running service means a 500 rather than "the
class just filled up". So the assertions below check not only that one student got in, but that
the other 19 were turned away *through the intended path* - if they came back as raw database
errors, the threads would die and the result count would not add up.
Muc dich cua cai khoa la thu do duoc, khong phai thu suy doan. Chay dung kich ban nay tren mot
ban sao cua execute_registration da bo FOR UPDATE thi ket cuc van la 1 sinh vien trong mot lop 1
cho: rang buoc CHECK giu duoc phong tuyen. Nhung 19 nguoi con lai that bai voi CheckViolation do
PostgreSQL nem ra, chu khong phai RegistrationRejected, ma trong dich vu dang chay thi dieu do
nghia la loi 500 thay vi cau "lop vua het cho". Vi vay cac phep khang dinh ben duoi kiem tra
khong chi rang mot sinh vien da vao duoc, ma con rang 19 nguoi kia bi tu choi *dung theo duong
da thiet ke* - neu ho quay ve duoi dang loi database tho, cac luong se chet va so ket qua dem
duoc se khong khop.

These tests need a real PostgreSQL: a lock that is never contended proves nothing.
Cac bai test nay can PostgreSQL that: mot cai khoa khong bao gio bi tranh chap thi khong chung
minh duoc dieu gi.

Chay: pytest tests/test_registration_concurrency.py -v
"""

import threading

import psycopg
import pytest
from psycopg.rows import dict_row

from app.agent.tools import RegistrationRejected, execute_registration
from app.config import Settings, load_settings

pytestmark = pytest.mark.integration

# More contenders than the database pool allows, on purpose: each thread opens its own
# connection, so the fight happens in PostgreSQL and not in a queue in front of it.
# Co y de so nguoi tranh nhau nhieu hon so ket noi trong pool: moi luong tu mo ket noi rieng,
# nen cuoc gianh giat dien ra trong PostgreSQL chu khong phai trong mot hang doi dung truoc no.
CONTENDERS = 20

SEMESTER = "9999.9"
COURSE_CODE = "TEST999"


@pytest.fixture
def settings() -> Settings:
    """The real settings, which is where the credit ceiling per academic status comes from.

    Cau hinh that, von la noi quyet dinh tran tin chi ung voi tung tinh trang hoc vu.

    The confirmation transaction re-runs the six registration rules under lock, and the credit
    ceiling is one of them, so it needs to know what the ceiling is.
    Transaction xac nhan chay lai sau quy tac dang ky duoi khoa, ma tran tin chi la mot trong so
    do, nen no can biet tran la bao nhieu.
    """
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
def contested_class(database_url: str):
    """One class with exactly one free seat, and CONTENDERS students each holding a slip for it.

    Mot lop con dung mot cho trong, va CONTENDERS sinh vien, moi nguoi cam mot phieu vao lop do.
    """
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)

        # Registration has to be open for the test semester. The confirmation transaction now
        # re-checks all six rules under lock, and "registration is open" is one of them, so a
        # semester with no window at all would see every confirmation refused for the wrong
        # reason and the test would pass while proving nothing.
        # Dot dang ky phai dang mo cho hoc ky kiem thu. Transaction xac nhan bay gio kiem tra lai
        # ca sau quy tac duoi khoa, ma "dang trong thoi gian mo dang ky" la mot trong so do, nen
        # mot hoc ky khong co khung thoi gian nao se khien moi lenh xac nhan bi tu choi vi mot ly
        # do khac, va bai test se pass ma khong chung minh duoc gi.
        _open_registration(conn)

        conn.execute(
            """
            INSERT INTO courses (course_code, course_name, credits, department, is_required)
            VALUES (%s, 'Hoc phan kiem thu', 3, 'Kiem thu', FALSE)
            """,
            (COURSE_CODE,),
        )
        section = conn.execute(
            """
            INSERT INTO class_sections (
                course_code, section_no, semester, lecturer, capacity, enrolled,
                day_of_week, start_period, end_period, room
            )
            VALUES (%s, '01', %s, 'GV kiem thu', 1, 0, 2, 1, 3, 'P.TEST')
            RETURNING id
            """,
            (COURSE_CODE, SEMESTER),
        ).fetchone()
        section_id = section["id"]

        slips = []
        for index in range(CONTENDERS):
            student_id = f"TEST{index:04d}"
            slip_id = f"DKTEST{index:04d}"
            conn.execute(
                """
                INSERT INTO students (student_id, full_name, major, cohort)
                VALUES (%s, %s, 'Kiem thu', 'KTEST')
                """,
                (student_id, f"Sinh vien kiem thu {index}"),
            )
            conn.execute(
                """
                INSERT INTO pending_registrations (
                    id, session_id, student_id, created_turn_id, class_section_id, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, now() + interval '10 minutes')
                """,
                (slip_id, f"session-{index}", student_id, f"turn-{index}", section_id),
            )
            slips.append(slip_id)

    yield section_id, slips

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)


def _open_registration(conn) -> None:
    conn.execute(
        """
        INSERT INTO registration_windows (semester, opens_at, closes_at)
        VALUES (%s, now() - interval '1 day', now() + interval '1 day')
        ON CONFLICT (semester) DO NOTHING
        """,
        (SEMESTER,),
    )


def _cleanup(conn) -> None:
    conn.execute("DELETE FROM pending_registrations WHERE student_id LIKE 'TEST%'")
    conn.execute("DELETE FROM enrollments WHERE student_id LIKE 'TEST%'")
    conn.execute("DELETE FROM students WHERE student_id LIKE 'TEST%'")
    conn.execute("DELETE FROM class_sections WHERE semester = %s", (SEMESTER,))
    conn.execute("DELETE FROM courses WHERE course_code LIKE 'TEST%'")
    conn.execute("DELETE FROM registration_windows WHERE semester = %s", (SEMESTER,))


def _confirm(
    database_url: str, slip_id: str, barrier: threading.Barrier, settings: Settings
) -> str:
    """Confirm one slip, having first waited for every other thread to be ready.

    Xac nhan mot phieu, sau khi da doi cho moi luong khac cung san sang.

    The barrier is the point of the whole test. Without it the threads would trickle in one
    after another and the lock would never actually be contended, so the test would pass even
    if the locking were wrong.
    Cai hang rao dong bo nay chinh la muc dich cua ca bai test. Neu khong co no, cac luong se
    lac dac vao tung cai mot va cai khoa se khong bao gio thuc su bi tranh chap, nen bai test
    van se pass ngay ca khi phan khoa bi sai.
    """
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        barrier.wait()
        try:
            with conn.transaction():
                execute_registration(conn, slip_id, settings)
            return "thanh_cong"
        except RegistrationRejected:
            return "bi_tu_choi"


def test_only_one_student_gets_the_last_seat(database_url, settings, contested_class):
    section_id, slips = contested_class
    barrier = threading.Barrier(CONTENDERS)
    results: list[str] = []
    lock = threading.Lock()

    def worker(slip_id: str) -> None:
        outcome = _confirm(database_url, slip_id, barrier, settings)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(slip,)) for slip in slips]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Every thread came back through one of the two intended outcomes. A thread that died on a
    # raw CheckViolation would never have appended anything, so a short list here is itself the
    # signal that the lock is not doing its job.
    # Moi luong deu quay ve qua mot trong hai ket cuc da thiet ke. Mot luong chet vi CheckViolation
    # tho se khong kip ghi lai gi ca, nen danh sach bi thieu o day tu no da la dau hieu cho thay
    # cai khoa khong lam dung viec cua no.
    assert len(results) == CONTENDERS, (
        f"Chi {len(results)}/{CONTENDERS} luong tra ve mot ket cuc co kiem soat. "
        "So con lai da chet vi loi database tho."
    )
    assert results.count("thanh_cong") == 1, (
        f"Dung mot sinh vien duoc nhan cho, nhung co {results.count('thanh_cong')}."
    )
    assert results.count("bi_tu_choi") == CONTENDERS - 1

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        section = conn.execute(
            "SELECT capacity, enrolled FROM class_sections WHERE id = %s", (section_id,)
        ).fetchone()
        enrolled = conn.execute(
            "SELECT COUNT(*) AS n FROM enrollments WHERE class_section_id = %s", (section_id,)
        ).fetchone()

    # The counter and the actual rows must agree. A class that says 1 of 1 while holding two
    # students in it is the failure this whole design exists to prevent.
    # Bo dem va so dong thuc te phai khop nhau. Mot lop ghi la 1 tren 1 nhung ben trong lai chua
    # hai sinh vien chinh la that bai ma toan bo thiet ke nay sinh ra de ngan chan.
    assert section["enrolled"] == 1
    assert section["enrolled"] <= section["capacity"]
    assert enrolled["n"] == 1


def test_confirming_the_same_slip_twice_enrols_the_student_once(
    database_url, settings, contested_class
):
    # A retried HTTP request, or a model that calls the tool twice, must not enrol the student
    # twice. The slip is claimed by the UPDATE itself, so the second attempt claims nothing.
    # Mot request HTTP bi gui lai, hay mot model goi tool hai lan, khong duoc phep ghi danh sinh
    # vien hai lan. Phieu duoc gianh bang chinh cau UPDATE, nen lan thu hai khong gianh duoc gi.
    section_id, slips = contested_class
    slip_id = slips[0]

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            first = execute_registration(conn, slip_id, settings)
        assert first["trang_thai"] == "dang_ky_thanh_cong"

        with pytest.raises(RegistrationRejected, match="khong con o trang thai cho xac nhan"):
            with conn.transaction():
                execute_registration(conn, slip_id, settings)

        section = conn.execute(
            "SELECT enrolled FROM class_sections WHERE id = %s", (section_id,)
        ).fetchone()

    assert section["enrolled"] == 1


CLASH_STUDENT = "TESTCLASH1"


@pytest.fixture
def clashing_classes(database_url: str):
    """One student, two roomy classes whose timetables overlap, and a slip for each.

    Mot sinh vien, hai lop con rong nhung lich hoc chong len nhau, va mot phieu cho moi lop.

    Nothing here is contested between students: both classes have 50 free seats, so the class
    lock has nothing to do. The contest is inside one student's own timetable, which is precisely
    the collision the class lock was never able to see.
    O day khong co gi bi tranh chap giua cac sinh vien: ca hai lop deu con 50 cho, nen khoa lop
    khong co viec gi de lam. Cuoc tranh chap nam ngay trong thoi khoa bieu cua chinh mot sinh vien,
    va do dung la va cham ma khoa lop khong bao gio nhin thay duoc.
    """
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)
        _open_registration(conn)

        conn.execute(
            """
            INSERT INTO students (student_id, full_name, major, cohort)
            VALUES (%s, 'Sinh vien trung lich', 'Kiem thu', 'KTEST')
            """,
            (CLASH_STUDENT,),
        )

        slips = []
        # Thursday, periods 1-3 and 2-4: they overlap on periods 2 and 3.
        # Thu 5, tiet 1-3 va tiet 2-4: chung chong nhau o tiet 2 va tiet 3.
        for index, (code, start, end) in enumerate([("TESTC1", 1, 3), ("TESTC2", 2, 4)]):
            conn.execute(
                """
                INSERT INTO courses (course_code, course_name, credits, department, is_required)
                VALUES (%s, %s, 3, 'Kiem thu', FALSE)
                """,
                (code, f"Hoc phan trung lich {index}"),
            )
            section = conn.execute(
                """
                INSERT INTO class_sections (
                    course_code, section_no, semester, lecturer, capacity, enrolled,
                    day_of_week, start_period, end_period, room
                )
                VALUES (%s, '01', %s, 'GV kiem thu', 50, 0, 5, %s, %s, 'P.TEST')
                RETURNING id
                """,
                (code, SEMESTER, start, end),
            ).fetchone()

            slip_id = f"DKCLASH{index}"
            conn.execute(
                """
                INSERT INTO pending_registrations (
                    id, session_id, student_id, created_turn_id, class_section_id, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, now() + interval '10 minutes')
                """,
                (slip_id, "session-clash", CLASH_STUDENT, f"turn-{index}", section["id"]),
            )
            slips.append(slip_id)

    yield slips

    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)


def test_one_student_cannot_confirm_two_clashing_classes_at_once(
    database_url, settings, clashing_classes
):
    """The hole the class lock never covered, and the student lock now does.

    Lo hong ma khoa lop khong bao gio cham toi, va gio khoa sinh vien bit lai.

    The guardrail reads the student's enrolments once, at the start of the turn, outside any
    transaction. Two confirmations racing for the same student both read that same empty list,
    both find no clash, and - before the student row was locked - both went through, leaving the
    student booked into two classes at the same hour of the same day.
    Guardrail doc danh sach lop cua sinh vien mot lan, tu dau luot, ben ngoai moi transaction. Hai
    lenh xac nhan chay dua cho cung mot sinh vien deu doc cung mot danh sach rong do, deu thay
    khong trung lich, va - truoc khi dong sinh vien duoc khoa - ca hai deu di qua, de lai sinh vien
    bi xep vao hai lop cung mot khung gio cua cung mot ngay.
    """
    slips = clashing_classes
    barrier = threading.Barrier(len(slips))
    results: list[str] = []
    lock = threading.Lock()

    def worker(slip_id: str) -> None:
        outcome = _confirm(database_url, slip_id, barrier, settings)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(slip,)) for slip in slips]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == len(slips), (
        f"Chi {len(results)}/{len(slips)} luong tra ve mot ket cuc co kiem soat."
    )
    assert results.count("thanh_cong") == 1, (
        "Dung mot lop duoc ghi danh, nhung co "
        f"{results.count('thanh_cong')}. Sinh vien vua bi xep vao hai lop trung lich."
    )
    assert results.count("bi_tu_choi") == 1

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        enrolments = conn.execute(
            "SELECT COUNT(*) AS n FROM enrollments WHERE student_id = %s",
            (CLASH_STUDENT,),
        ).fetchone()

    # The claim that matters, read from the database rather than from the threads' own report.
    # Phep khang dinh quan trong nhat, doc tu database chu khong doc tu bao cao cua chinh cac luong.
    assert enrolments["n"] == 1, (
        f"Sinh vien co {enrolments['n']} lop trong hoc ky, dang le chi duoc 1: "
        "hai lop trung lich da cung lot qua."
    )


def test_database_refuses_to_overfill_a_class_even_if_the_code_is_wrong(
    database_url, contested_class
):
    # The last line of defence, below all the application logic: the CHECK constraint. If a
    # future refactor ever dropped the lock, or miscounted, PostgreSQL would still not let the
    # class hold more students than it has seats.
    # Lop phong thu cuoi cung, nam duoi moi logic ung dung: rang buoc CHECK. Neu mot lan refactor
    # nao do sau nay lam mat cai khoa, hoac dem sai, PostgreSQL van khong cho lop chua nhieu sinh
    # vien hon so cho no co.
    section_id, _ = contested_class

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE class_sections SET enrolled = capacity + 1 WHERE id = %s",
                (section_id,),
            )
