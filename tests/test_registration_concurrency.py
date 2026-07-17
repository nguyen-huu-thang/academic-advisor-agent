"""What happens when several students go for the last seat at the same instant.

Chuyện gì xảy ra khi nhiều sinh viên cùng lao vào chỗ ngồi cuối cùng trong cùng một khoảnh khắc.

The guardrail reads the seat count outside any transaction, a moment before the model is even
asked what to do. By the time a confirmation actually runs, that number may already be stale.
So the seat is counted a second time, under a row lock, inside the transaction that writes the
enrolment - and it is that second count which decides.
Guardrail đọc sĩ số bên ngoài mọi transaction, từ trước khi model được hỏi phải làm gì. Đến lúc
một lệnh xác nhận thực sự chạy, con số đó có thể đã cũ. Vì vậy chỗ ngồi được đếm lại lần thứ hai,
dưới một khóa dòng, bên trong chính transaction ghi bản đăng ký - và lần đếm thứ hai đó mới là
lần quyết định.

What the lock is for was measured, not assumed. Running this same scenario against a copy of
execute_registration with FOR UPDATE taken out still ends with 1 student in a 1-seat class: the
CHECK constraint holds the line. But the other 19 fail with a CheckViolation from PostgreSQL
instead of a RegistrationRejected, which in the running service means a 500 rather than "the
class just filled up". So the assertions below check not only that one student got in, but that
the other 19 were turned away *through the intended path* - if they came back as raw database
errors, the threads would die and the result count would not add up.
Mục đích của cái khóa là thứ đo được, không phải thứ suy đoán. Chạy đúng kịch bản này trên một
bản sao của execute_registration đã bỏ FOR UPDATE thì kết cục vẫn là 1 sinh viên trong một lớp 1
chỗ: ràng buộc CHECK giữ được phòng tuyến. Nhưng 19 người còn lại thất bại với CheckViolation do
PostgreSQL ném ra, chứ không phải RegistrationRejected, mà trong dịch vụ đang chạy thì điều đó
nghĩa là lỗi 500 thay vì câu "lớp vừa hết chỗ". Vì vậy các phép khẳng định bên dưới kiểm tra
không chỉ rằng một sinh viên đã vào được, mà còn rằng 19 người kia bị từ chối *đúng theo đường
đã thiết kế* - nếu họ quay về dưới dạng lỗi database thô, các luồng sẽ chết và số kết quả đếm
được sẽ không khớp.

These tests need a real PostgreSQL: a lock that is never contended proves nothing.
Các bài test này cần PostgreSQL thật: một cái khóa không bao giờ bị tranh chấp thì không chứng
minh được điều gì.

Chạy: pytest tests/test_registration_concurrency.py -v
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
# Cố ý để số người tranh nhau nhiều hơn số kết nối trong pool: mỗi luồng tự mở kết nối riêng,
# nên cuộc giành giật diễn ra trong PostgreSQL chứ không phải trong một hàng đợi đứng trước nó.
CONTENDERS = 20

SEMESTER = "9999.9"
COURSE_CODE = "TEST999"


@pytest.fixture
def settings() -> Settings:
    """The real settings, which is where the credit ceiling per academic status comes from.

    Cấu hình thật, vốn là nơi quyết định trần tín chỉ ứng với từng tình trạng học vụ.

    The confirmation transaction re-runs the six registration rules under lock, and the credit
    ceiling is one of them, so it needs to know what the ceiling is.
    Transaction xác nhận chạy lại sáu quy tắc đăng ký dưới khóa, mà trần tín chỉ là một trong số
    đó, nên nó cần biết trần là bao nhiêu.
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

    Một lớp còn đúng một chỗ trống, và CONTENDERS sinh viên, mỗi người cầm một phiếu vào lớp đó.
    """
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        _cleanup(conn)

        # Registration has to be open for the test semester. The confirmation transaction now
        # re-checks all six rules under lock, and "registration is open" is one of them, so a
        # semester with no window at all would see every confirmation refused for the wrong
        # reason and the test would pass while proving nothing.
        # Đợt đăng ký phải đang mở cho học kỳ kiểm thử. Transaction xác nhận bây giờ kiểm tra lại
        # cả sáu quy tắc dưới khóa, mà "đang trong thời gian mở đăng ký" là một trong số đó, nên
        # một học kỳ không có khung thời gian nào sẽ khiến mọi lệnh xác nhận bị từ chối vì một lý
        # do khác, và bài test sẽ pass mà không chứng minh được gì.
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

    Xác nhận một phiếu, sau khi đã đợi cho mọi luồng khác cùng sẵn sàng.

    The barrier is the point of the whole test. Without it the threads would trickle in one
    after another and the lock would never actually be contended, so the test would pass even
    if the locking were wrong.
    Cái hàng rào đồng bộ này chính là mục đích của cả bài test. Nếu không có nó, các luồng sẽ
    lác đác vào từng cái một và cái khóa sẽ không bao giờ thực sự bị tranh chấp, nên bài test
    vẫn sẽ pass ngay cả khi phần khóa bị sai.
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
    # Mọi luồng đều quay về qua một trong hai kết cục đã thiết kế. Một luồng chết vì CheckViolation
    # thô sẽ không kịp ghi lại gì cả, nên danh sách bị thiếu ở đây tự nó đã là dấu hiệu cho thấy
    # cái khóa không làm đúng việc của nó.
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
    # Bộ đếm và số dòng thực tế phải khớp nhau. Một lớp ghi là 1 trên 1 nhưng bên trong lại chứa
    # hai sinh viên chính là thất bại mà toàn bộ thiết kế này sinh ra để ngăn chặn.
    assert section["enrolled"] == 1
    assert section["enrolled"] <= section["capacity"]
    assert enrolled["n"] == 1


def test_confirming_the_same_slip_twice_enrols_the_student_once(
    database_url, settings, contested_class
):
    # A retried HTTP request, or a model that calls the tool twice, must not enrol the student
    # twice. The slip is claimed by the UPDATE itself, so the second attempt claims nothing.
    # Một request HTTP bị gửi lại, hay một model gọi tool hai lần, không được phép ghi danh sinh
    # viên hai lần. Phiếu được giành bằng chính câu UPDATE, nên lần thứ hai không giành được gì.
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

    Một sinh viên, hai lớp còn rộng nhưng lịch học chồng lên nhau, và một phiếu cho mỗi lớp.

    Nothing here is contested between students: both classes have 50 free seats, so the class
    lock has nothing to do. The contest is inside one student's own timetable, which is precisely
    the collision the class lock was never able to see.
    Ở đây không có gì bị tranh chấp giữa các sinh viên: cả hai lớp đều còn 50 chỗ, nên khóa lớp
    không có việc gì để làm. Cuộc tranh chấp nằm ngay trong thời khóa biểu của chính một sinh viên,
    và đó đúng là va chạm mà khóa lớp không bao giờ nhìn thấy được.
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
        # Thứ 5, tiết 1-3 và tiết 2-4: chúng chồng nhau ở tiết 2 và tiết 3.
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

    Lỗ hổng mà khóa lớp không bao giờ chạm tới, và giờ khóa sinh viên bịt lại.

    The guardrail reads the student's enrolments once, at the start of the turn, outside any
    transaction. Two confirmations racing for the same student both read that same empty list,
    both find no clash, and - before the student row was locked - both went through, leaving the
    student booked into two classes at the same hour of the same day.
    Guardrail đọc danh sách lớp của sinh viên một lần, từ đầu lượt, bên ngoài mọi transaction. Hai
    lệnh xác nhận chạy đua cho cùng một sinh viên đều đọc cùng một danh sách rỗng đó, đều thấy
    không trùng lịch, và - trước khi dòng sinh viên được khóa - cả hai đều đi qua, để lại sinh viên
    bị xếp vào hai lớp cùng một khung giờ của cùng một ngày.
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
    # Phép khẳng định quan trọng nhất, đọc từ database chứ không đọc từ báo cáo của chính các luồng.
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
    # Lớp phòng thủ cuối cùng, nằm dưới mọi logic ứng dụng: ràng buộc CHECK. Nếu một lần refactor
    # nào đó sau này làm mất cái khóa, hoặc đếm sai, PostgreSQL vẫn không cho lớp chứa nhiều sinh
    # viên hơn số chỗ nó có.
    section_id, _ = contested_class

    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE class_sections SET enrolled = capacity + 1 WHERE id = %s",
                (section_id,),
            )
