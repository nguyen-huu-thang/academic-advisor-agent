"""Create the schema and seed the simulated student records.

Tao luoc do va nap du lieu sinh vien mo phong.

The courses, credits and prerequisites here must match data/documents/chuong-trinh-dao-tao.md,
and the grading rules come from app/grading.py which matches data/documents/quy-che-dao-tao.md.
If they drifted apart, the assistant would quote one rule from the regulation and then enforce
a different one from the database.
Danh sach mon, so tin chi va quan he tien quyet o day phai khop voi
data/documents/chuong-trinh-dao-tao.md, con quy tac tinh diem lay tu app/grading.py von da khop
voi data/documents/quy-che-dao-tao.md. Neu chung lech nhau, tro ly se trich mot quy tac tu quy
che roi lai ap dung mot quy tac khac tu database.

Chay: python -m scripts.init_db
"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config import load_settings
from app.db import close_pool, get_connection
from app.grading import compute_gpa, earned_credits, is_passed

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "app" / "schema.sql"

# (course_code, course_name, credits, department, is_required)
COURSES = [
    ("MAT1041", "Giai tich 1", 4, "Toan", True),
    ("MAT1093", "Dai so tuyen tinh", 3, "Toan", True),
    ("MAT1101", "Toan roi rac", 3, "Toan", True),
    ("MAT1042", "Giai tich 2", 4, "Toan", True),
    ("MAT1104", "Xac suat thong ke", 3, "Toan", True),
    ("INT1008", "Nhap mon lap trinh", 3, "Cong nghe thong tin", True),
    ("INT2010", "Cau truc du lieu va giai thuat", 4, "Cong nghe thong tin", True),
    ("INT2011", "Kien truc may tinh", 3, "Cong nghe thong tin", True),
    ("INT2204", "Lap trinh huong doi tuong", 3, "Cong nghe thong tin", True),
    ("INT2207", "Co so du lieu", 3, "Cong nghe thong tin", True),
    ("INT2208", "Mang may tinh", 3, "Cong nghe thong tin", True),
    ("INT3110", "He dieu hanh", 3, "Cong nghe thong tin", True),
    ("INT3401", "Tri tue nhan tao", 3, "Cong nghe thong tin", True),
    ("INT3405", "Hoc may", 3, "Cong nghe thong tin", False),
    ("INT3306", "Phat trien ung dung web", 3, "Cong nghe thong tin", False),
    ("INT3117", "Kiem thu phan mem", 3, "Cong nghe thong tin", False),
    ("INT3502", "An toan thong tin", 3, "Cong nghe thong tin", False),
]

# (course_code, prereq_code)
PREREQUISITES = [
    ("MAT1042", "MAT1041"),
    ("MAT1104", "MAT1041"),
    ("INT2010", "INT1008"),
    ("INT2011", "INT1008"),
    ("INT2204", "INT1008"),
    ("INT2207", "INT2010"),
    ("INT2208", "INT2011"),
    ("INT3110", "INT2011"),
    # Artificial Intelligence needs both discrete maths and data structures. This is the
    # prerequisite the demo tries to talk its way past.
    # Tri tue nhan tao can ca Toan roi rac lan Cau truc du lieu. Day chinh la mon tien quyet
    # ma kich ban demo se tim cach noi khich de vuot qua.
    ("INT3401", "MAT1101"),
    ("INT3401", "INT2010"),
    ("INT3405", "MAT1104"),
    ("INT3405", "INT2010"),
    ("INT3306", "INT2207"),
    ("INT3117", "INT2204"),
    ("INT3502", "INT2208"),
]

# (course_code, section_no, lecturer, capacity, enrolled, day_of_week, start, end, room)
# `enrolled` is the number of students already in the class who are not among the three
# seeded below; the seeded enrolments are added on top afterwards.
# `enrolled` la so sinh vien da co trong lop nhung khong nam trong ba sinh vien duoc seed ben
# duoi; cac dong dang ky cua ho se duoc cong them vao sau.
SECTIONS = [
    ("INT3401", "01", "TS. Nguyen Van Hung", 60, 45, 3, 1, 3, "GD2-301"),
    # Full on purpose: registering here must be refused however the request is phrased.
    # Co y de day: dang ky vao lop nay phai bi tu choi du yeu cau duoc dien dat kieu gi.
    ("INT3401", "02", "TS. Nguyen Van Hung", 50, 50, 5, 7, 9, "GD2-302"),
    # Clashes with INT3401 section 01: same weekday, periods 2-4 overlap periods 1-3.
    # Trung lich voi INT3401 lop 01: cung thu, tiet 2-4 giao voi tiet 1-3.
    ("INT3405", "01", "PGS.TS. Tran Thu Ha", 40, 20, 3, 2, 4, "GD2-305"),
    ("INT3306", "01", "ThS. Le Quang Vinh", 50, 30, 2, 1, 3, "GD1-201"),
    ("INT3117", "01", "ThS. Pham Thi Mai", 40, 10, 2, 7, 9, "GD1-202"),
    # One seat left: this is the class the concurrency test fights over.
    # Chi con mot cho: day la lop ma bai test tranh chap dong thoi se gianh nhau.
    ("INT3502", "01", "TS. Do Minh Tuan", 45, 44, 4, 7, 9, "GD2-401"),
    ("MAT1101", "01", "TS. Vu Thi Lan", 80, 50, 5, 1, 3, "GD3-101"),
    ("MAT1093", "01", "TS. Vu Thi Lan", 80, 60, 6, 1, 3, "GD3-102"),
    ("MAT1042", "01", "TS. Hoang Van Nam", 70, 40, 2, 4, 6, "GD3-103"),
    ("MAT1104", "01", "TS. Hoang Van Nam", 70, 40, 6, 7, 9, "GD3-104"),
    ("INT2010", "01", "TS. Nguyen Van Hung", 60, 35, 4, 1, 3, "GD1-301"),
    ("INT2204", "01", "ThS. Le Quang Vinh", 60, 30, 5, 4, 6, "GD1-302"),
    ("INT2011", "01", "TS. Do Minh Tuan", 60, 25, 6, 4, 6, "GD1-303"),
    ("INT2207", "01", "ThS. Pham Thi Mai", 60, 55, 4, 4, 6, "GD1-304"),
    ("INT3110", "01", "TS. Do Minh Tuan", 60, 40, 6, 1, 3, "GD1-305"),
]

# (student_id, full_name, major, cohort)
# GPA, credits earned and academic status are computed from the grades below rather than
# written down here, so the three can never contradict each other.
# GPA, so tin chi tich luy va tinh trang hoc vu duoc tinh tu bang diem ben duoi chu khong ghi
# san o day, nen ba con so nay khong bao gio co the mau thuan voi nhau.
STUDENTS = [
    ("22021001", "Nguyen Van An", "Cong nghe thong tin", "K67"),
    ("22021002", "Tran Thi Binh", "Cong nghe thong tin", "K67"),
    ("22021003", "Le Minh Cuong", "Cong nghe thong tin", "K67"),
]

# student_id -> [(course_code, semester, score)]
GRADES: dict[str, list[tuple[str, str, str]]] = {
    # An is a solid student with one hole in his record: he failed Discrete Maths, which is
    # a prerequisite of Artificial Intelligence. Everything in the demo hangs off this 3.5.
    # An hoc kha, chi co dung mot lo hong trong bang diem: em truot Toan roi rac, von la mon
    # tien quyet cua Tri tue nhan tao. Toan bo kich ban demo xoay quanh con 3.5 nay.
    "22021001": [
        ("INT1008", "2024.1", "8.0"),
        ("MAT1041", "2024.1", "7.5"),
        ("MAT1093", "2024.1", "7.0"),
        ("MAT1101", "2024.2", "3.5"),
        ("MAT1042", "2024.2", "6.5"),
        ("MAT1104", "2025.1", "7.0"),
        ("INT2010", "2024.2", "8.5"),
        ("INT2011", "2025.1", "7.5"),
        ("INT2204", "2025.1", "8.0"),
        ("INT2207", "2025.2", "7.0"),
    ],
    # Binh is on academic warning: her GPA is below 2.0, so her credit ceiling drops to 18.
    # Binh dang bi canh bao hoc vu: GPA duoi 2.0, nen tran tin chi cua em tut xuong con 18.
    "22021002": [
        ("INT1008", "2024.1", "5.5"),
        ("MAT1041", "2024.1", "4.5"),
        ("MAT1093", "2024.1", "4.0"),
        ("MAT1101", "2024.2", "5.0"),
    ],
    # Cuong has passed everything, so he is the one who can actually register and therefore
    # the one who runs into the timetable clash and the full class.
    # Cuong da dat het cac mon, nen em la nguoi thuc su dang ky duoc, va cung la nguoi se gap
    # tinh huong trung lich va lop da day.
    "22021003": [
        ("INT1008", "2024.1", "9.0"),
        ("MAT1041", "2024.1", "8.5"),
        ("MAT1093", "2024.1", "8.0"),
        ("MAT1101", "2024.2", "8.5"),
        ("MAT1042", "2024.2", "8.0"),
        ("MAT1104", "2025.1", "9.0"),
        ("INT2010", "2024.2", "9.5"),
        ("INT2011", "2025.1", "8.0"),
        ("INT2204", "2025.1", "8.5"),
        ("INT2207", "2025.2", "9.0"),
        ("INT2208", "2025.2", "8.0"),
        ("INT3110", "2025.2", "8.5"),
    ],
}

# Classes already registered for the current semester, as (student_id, course_code, section_no).
# Binh sits at 17 credits, two below her ceiling of 18, so any further 3-credit course pushes
# her over it. That is the credit-ceiling scenario in the demo.
# Cac lop da dang ky san cho hoc ky hien tai. Binh dang o 17 tin chi, kem tran 18 dung 1 tin,
# nen bat ky mon 3 tin chi nao dang ky them cung se lam em vuot tran. Do la kich ban tran tin
# chi trong demo.
EXISTING_ENROLMENTS = [
    ("22021002", "MAT1042", "01"),
    ("22021002", "MAT1104", "01"),
    ("22021002", "INT2010", "01"),
    ("22021002", "INT2204", "01"),
    ("22021002", "INT2011", "01"),
]

# The academic warning thresholds, matching data/documents/quy-che-dao-tao.md.
# Cac nguong canh bao hoc vu, khop voi data/documents/quy-che-dao-tao.md.
WARNING_THRESHOLD = Decimal("2.0")


def academic_status(gpa: Decimal) -> str:
    """Derive the academic status from the cumulative GPA.

    Suy ra tinh trang hoc vu tu diem trung binh chung tich luy.

    A real registrar would also look at the previous semester's result to tell warning level
    one from level two. There is no semester history here, so the seed only distinguishes
    "warned" from "not warned" and leaves level two to be set by hand when a scenario needs it.
    Phong dao tao that con phai nhin ket qua ky truoc de phan biet canh bao muc 1 voi muc 2. O
    day khong co lich su hoc ky, nen du lieu seed chi phan biet "bi canh bao" va "khong bi canh
    bao", con muc 2 thi dat tay khi nao kich ban can toi.
    """
    return "binh_thuong" if gpa >= WARNING_THRESHOLD else "canh_bao_1"


def main() -> None:
    settings = load_settings()
    semester = settings.current_semester
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    credits_of = {code: credits for code, _, credits, _, _ in COURSES}

    with get_connection() as conn:
        conn.execute(schema_sql)

        # Order matters: children before parents, or the foreign keys refuse the delete.
        # Thu tu quan trong: xoa bang con truoc bang cha, neu khong khoa ngoai se chan lai.
        conn.execute("DELETE FROM tool_audit_log")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM pending_registrations")
        conn.execute("DELETE FROM enrollments")
        conn.execute("DELETE FROM grades")
        conn.execute("DELETE FROM class_sections")
        conn.execute("DELETE FROM prerequisites")
        conn.execute("DELETE FROM courses")
        conn.execute("DELETE FROM students")
        conn.execute("DELETE FROM registration_windows")

        conn.cursor().executemany(
            """
            INSERT INTO courses (course_code, course_name, credits, department, is_required)
            VALUES (%s, %s, %s, %s, %s)
            """,
            COURSES,
        )

        conn.cursor().executemany(
            "INSERT INTO prerequisites (course_code, prereq_code) VALUES (%s, %s)",
            PREREQUISITES,
        )

        conn.cursor().executemany(
            """
            INSERT INTO class_sections (
                course_code, section_no, semester, lecturer, capacity, enrolled,
                day_of_week, start_period, end_period, room
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (course, section, semester, lecturer, capacity, enrolled, day, start, end, room)
                for course, section, lecturer, capacity, enrolled, day, start, end, room
                in SECTIONS
            ],
        )

        for student_id, full_name, major, cohort in STUDENTS:
            entries = [
                (Decimal(score), credits_of[course])
                for course, _, score in GRADES[student_id]
            ]
            gpa = compute_gpa(entries)

            conn.execute(
                """
                INSERT INTO students (
                    student_id, full_name, major, cohort, gpa, credits_earned, academic_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    student_id,
                    full_name,
                    major,
                    cohort,
                    gpa,
                    earned_credits(entries),
                    academic_status(gpa),
                ),
            )

            conn.cursor().executemany(
                """
                INSERT INTO grades (student_id, course_code, semester, score, passed)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (student_id, course, term, score, is_passed(Decimal(score)))
                    for course, term, score in GRADES[student_id]
                ],
            )

        # Registering a seeded student bumps the class counter, exactly as a real
        # registration would. Writing the row without the bump would leave `enrolled`
        # disagreeing with the rows in `enrollments` from the very first day.
        # Ghi mot dong dang ky cho sinh vien seed thi cung phai tang bo dem cua lop, dung nhu
        # mot lan dang ky that. Neu chi ghi dong ma quen tang bo dem, `enrolled` se lech voi
        # cac dong trong `enrollments` ngay tu ngay dau tien.
        for student_id, course_code, section_no in EXISTING_ENROLMENTS:
            section = conn.execute(
                """
                SELECT id FROM class_sections
                WHERE course_code = %s AND section_no = %s AND semester = %s
                """,
                (course_code, section_no, semester),
            ).fetchone()

            conn.execute(
                """
                INSERT INTO enrollments (student_id, class_section_id, course_code, semester)
                VALUES (%s, %s, %s, %s)
                """,
                (student_id, section["id"], course_code, semester),
            )
            conn.execute(
                "UPDATE class_sections SET enrolled = enrolled + 1 WHERE id = %s",
                (section["id"],),
            )

        now = datetime.now()
        conn.execute(
            """
            INSERT INTO registration_windows (semester, opens_at, closes_at)
            VALUES (%s, %s, %s)
            """,
            (semester, now - timedelta(days=7), now + timedelta(days=14)),
        )

        rows = conn.execute(
            "SELECT student_id, full_name, gpa, credits_earned, academic_status "
            "FROM students ORDER BY student_id"
        ).fetchall()

    print(
        f"Da tao schema va nap {len(COURSES)} hoc phan, {len(PREREQUISITES)} quan he tien quyet, "
        f"{len(SECTIONS)} lop hoc phan cho hoc ky {semester}."
    )
    print(f"Dang ky hoc phan dang mo den {(now + timedelta(days=14)).strftime('%d/%m/%Y')}.\n")
    for row in rows:
        print(
            f"  {row['student_id']}  {row['full_name']:<18}"
            f"GPA {row['gpa']}  {row['credits_earned']:>3} tin chi  {row['academic_status']}"
        )
    close_pool()


if __name__ == "__main__":
    main()
