"""Create the schema and seed the simulated student records.

Tạo lược đồ và nạp dữ liệu sinh viên mô phỏng.

The courses, credits and prerequisites here must match data/documents/chuong-trinh-dao-tao.md,
and the grading rules come from app/grading.py which matches data/documents/quy-che-dao-tao.md.
If they drifted apart, the assistant would quote one rule from the regulation and then enforce
a different one from the database.
Danh sách môn, số tín chỉ và quan hệ tiên quyết ở đây phải khớp với
data/documents/chuong-trinh-dao-tao.md, còn quy tắc tính điểm lấy từ app/grading.py vốn đã khớp
với data/documents/quy-che-dao-tao.md. Nếu chúng lệch nhau, trợ lý sẽ trích một quy tắc từ quy
chế rồi lại áp dụng một quy tắc khác từ database.

Chạy: python -m scripts.init_db
"""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.auth.passwords import hash_password
from app.config import load_settings
from app.db import close_pool, get_connection
from app.grading import compute_gpa, earned_credits, is_passed

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "app" / "schema.sql"

# The password the three simulated students share, so the demo and the README have something to
# log in with. A real university issues each student their own and never prints it anywhere; this
# is seed data for a simulation, and the only reason it can be written down here is that these
# three students do not exist.
# Mật khẩu dùng chung của ba sinh viên mô phỏng, để bản demo và README có cái mà đăng nhập. Một
# trường đại học thật thì cấp cho mỗi sinh viên một mật khẩu riêng và không bao giờ in nó ra ở
# đâu cả; đây là dữ liệu mô phỏng, và lý do duy nhất viết được nó ra đây là ba sinh viên này
# không có thật.
DEMO_PASSWORD = "Sinhvien@2026"

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
    # Trí tuệ nhân tạo cần cả Toán rời rạc lẫn Cấu trúc dữ liệu. Đây chính là môn tiên quyết
    # mà kịch bản demo sẽ tìm cách nói khích để vượt qua.
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
# `enrolled` là số sinh viên đã có trong lớp nhưng không nằm trong ba sinh viên được seed bên
# dưới; các dòng đăng ký của họ sẽ được cộng thêm vào sau.
SECTIONS = [
    ("INT3401", "01", "TS. Nguyen Van Hung", 60, 45, 3, 1, 3, "GD2-301"),
    # Full on purpose: registering here must be refused however the request is phrased.
    # Cố ý để đầy: đăng ký vào lớp này phải bị từ chối dù yêu cầu được diễn đạt kiểu gì.
    ("INT3401", "02", "TS. Nguyen Van Hung", 50, 50, 5, 7, 9, "GD2-302"),
    # Clashes with INT3401 section 01: same weekday, periods 2-4 overlap periods 1-3.
    # Trùng lịch với INT3401 lớp 01: cùng thứ, tiết 2-4 giao với tiết 1-3.
    ("INT3405", "01", "PGS.TS. Tran Thu Ha", 40, 20, 3, 2, 4, "GD2-305"),
    ("INT3306", "01", "ThS. Le Quang Vinh", 50, 30, 2, 1, 3, "GD1-201"),
    ("INT3117", "01", "ThS. Pham Thi Mai", 40, 10, 2, 7, 9, "GD1-202"),
    # One seat left: this is the class the concurrency test fights over.
    # Chỉ còn một chỗ: đây là lớp mà bài test tranh chấp đồng thời sẽ giành nhau.
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
# GPA, số tín chỉ tích lũy và tình trạng học vụ được tính từ bảng điểm bên dưới chứ không ghi
# sẵn ở đây, nên ba con số này không bao giờ có thể mâu thuẫn với nhau.
STUDENTS = [
    ("22021001", "Nguyen Van An", "Cong nghe thong tin", "K67"),
    ("22021002", "Tran Thi Binh", "Cong nghe thong tin", "K67"),
    ("22021003", "Le Minh Cuong", "Cong nghe thong tin", "K67"),
]

# student_id -> [(course_code, semester, score)]
GRADES: dict[str, list[tuple[str, str, str]]] = {
    # An is a solid student with one hole in his record: he failed Discrete Maths, which is
    # a prerequisite of Artificial Intelligence. Everything in the demo hangs off this 3.5.
    # An học khá, chỉ có đúng một lỗ hổng trong bảng điểm: em trượt Toán rời rạc, vốn là môn
    # tiên quyết của Trí tuệ nhân tạo. Toàn bộ kịch bản demo xoay quanh con 3.5 này.
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
    # Bình đang bị cảnh báo học vụ: GPA dưới 2.0, nên trần tín chỉ của em tụt xuống còn 18.
    "22021002": [
        ("INT1008", "2024.1", "5.5"),
        ("MAT1041", "2024.1", "4.5"),
        ("MAT1093", "2024.1", "4.0"),
        ("MAT1101", "2024.2", "5.0"),
    ],
    # Cuong has passed everything, so he is the one who can actually register and therefore
    # the one who runs into the timetable clash and the full class.
    # Cường đã đạt hết các môn, nên em là người thực sự đăng ký được, và cũng là người sẽ gặp
    # tình huống trùng lịch và lớp đã đầy.
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
# Binh sits at 17 credits, one below her ceiling of 18, so any further 3-credit course pushes
# her over it. That is the credit-ceiling scenario in the demo.
# Các lớp đã đăng ký sẵn cho học kỳ hiện tại. Bình đang ở 17 tín chỉ, kém trần 18 đúng 1 tín,
# nên bất kỳ môn 3 tín chỉ nào đăng ký thêm cũng sẽ làm em vượt trần. Đó là kịch bản trần tín
# chỉ trong demo.
EXISTING_ENROLMENTS = [
    ("22021002", "MAT1042", "01"),
    ("22021002", "MAT1104", "01"),
    ("22021002", "INT2010", "01"),
    ("22021002", "INT2204", "01"),
    ("22021002", "INT2011", "01"),
]

# The academic warning thresholds, matching data/documents/quy-che-dao-tao.md.
# Các ngưỡng cảnh báo học vụ, khớp với data/documents/quy-che-dao-tao.md.
WARNING_THRESHOLD = Decimal("2.0")


def academic_status(gpa: Decimal) -> str:
    """Derive the academic status from the cumulative GPA.

    Suy ra tình trạng học vụ từ điểm trung bình chung tích lũy.

    A real registrar would also look at the previous semester's result to tell warning level
    one from level two. There is no semester history here, so the seed only distinguishes
    "warned" from "not warned" and leaves level two to be set by hand when a scenario needs it.
    Phòng đào tạo thật còn phải nhìn kết quả kỳ trước để phân biệt cảnh báo mức 1 với mức 2. Ở
    đây không có lịch sử học kỳ, nên dữ liệu seed chỉ phân biệt "bị cảnh báo" và "không bị cảnh
    báo", còn mức 2 thì đặt tay khi nào kịch bản cần tới.
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
        # Thứ tự quan trọng: xóa bảng con trước bảng cha, nếu không khóa ngoại sẽ chặn lại.
        conn.execute("DELETE FROM tool_audit_log")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM refresh_tokens")
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
            # GPA and earned credits are computed from the seeded grades with the very same
            # functions the tools use, so the seed can never disagree with the regulation.
            # GPA và tín chỉ tích lũy được tính từ bảng điểm seed bằng chính các hàm mà tool
            # dùng, nên dữ liệu seed không bao giờ lệch với quy chế.
            entries = [
                (Decimal(score), credits_of[course])
                for course, _, score in GRADES[student_id]
            ]
            gpa = compute_gpa(entries)

            conn.execute(
                """
                INSERT INTO students (
                    student_id, full_name, major, cohort, password_hash,
                    gpa, credits_earned, academic_status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    student_id,
                    full_name,
                    major,
                    cohort,
                    # Hashed once per student rather than hashed once and reused, so that three
                    # students with the same password still end up with three different hashes.
                    # That is what the salt is for, and seeding is no reason to skip it.
                    # Băm riêng cho từng sinh viên thay vì băm một lần rồi dùng lại, để ba sinh
                    # viên cùng mật khẩu vẫn cho ra ba bản băm khác nhau. Salt sinh ra là để làm
                    # việc đó, và việc nạp dữ liệu mẫu không phải lý do để bỏ qua nó.
                    hash_password(DEMO_PASSWORD),
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
        # Ghi một dòng đăng ký cho sinh viên seed thì cũng phải tăng bộ đếm của lớp, đúng như
        # một lần đăng ký thật. Nếu chỉ ghi dòng mà quên tăng bộ đếm, `enrolled` sẽ lệch với
        # các dòng trong `enrollments` ngay từ ngày đầu tiên.
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

        # The registration window opened a week ago and closes in two weeks, so the demo
        # always runs inside an open window.
        # Đợt đăng ký mở từ một tuần trước và đóng sau hai tuần nữa, nên demo luôn chạy
        # trong lúc đợt đăng ký đang mở.
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
    print(f"\nMat khau dang nhap cua ca ba sinh vien mo phong: {DEMO_PASSWORD}")
    close_pool()


if __name__ == "__main__":
    main()
