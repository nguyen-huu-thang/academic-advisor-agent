"""The tools the agent may call, and their implementations.

Các tool mà agent được phép gọi, kèm phần cài đặt.

Each tool is declared to Gemini as a function schema; the model reads the descriptions to
decide which one answers the student's question. The implementations read from PostgreSQL, so
every number the assistant quotes comes from real data rather than the model's own memory.
Mỗi tool được khai báo với Gemini dưới dạng function schema; model đọc phần mô tả để tự quyết
định gọi tool nào. Phần cài đặt đọc dữ liệu từ PostgreSQL, nên mọi con số mà trợ lý đưa ra đều
đến từ dữ liệu thật chứ không phải model tự nhớ.

Registering is split across two tools on purpose. `dang_ky_hoc_phan` writes the request down
and hands back a slip code; `xac_nhan_dang_ky` is the only one that puts the student in the
class. The split is what turns "the student agreed" into something the service can check
rather than something the model can claim - see guardrail.py.
Việc đăng ký được tách làm hai tool là có chủ đích. `dang_ky_hoc_phan` chỉ ghi lại nguyện vọng
và trả về một mã phiếu; `xac_nhan_dang_ky` mới là tool duy nhất đưa sinh viên vào lớp. Chính
việc tách đôi này biến "sinh viên đã đồng ý" thành thứ mà dịch vụ kiểm tra được, thay vì thứ mà
model muốn nói sao cũng được - xem guardrail.py.

The tools that read a student's record take no student id. The assistant serves one
authenticated student per session, so the id comes from the TurnContext. A field the model
cannot fill in is a field it cannot abuse.
Các tool đọc hồ sơ sinh viên không nhận tham số mã sinh viên. Trợ lý chỉ phục vụ đúng một sinh
viên đã xác thực trong mỗi phiên, nên mã sinh viên lấy từ TurnContext. Một ô trống mà model
không điền được thì cũng là một ô trống nó không lạm dụng sai được.
"""

import secrets
from decimal import Decimal

from google.genai import types

from app.agent.guardrail import (
    ClassSection,
    PendingRegistration,
    RegisteredClass,
    TurnContext,
    check_registration_rules,
)
from app.config import Settings
from app.db import get_connection
from app.grading import PASS_MARK, compute_gpa, grade_point, is_passed, letter_grade
from app.rag.retriever import Retriever

# How long the student has to confirm before the prepared registration goes stale. Long enough
# to read the class details and reply, short enough that a forgotten slip cannot be confirmed
# by accident hours later.
# Sinh viên có bao lâu để xác nhận trước khi phiếu đã chuẩn bị bị coi là cũ. Đủ dài để đọc thông
# tin lớp và trả lời, đủ ngắn để một phiếu bị bỏ quên không thể bị xác nhận nhầm sau nhiều giờ.
CONFIRM_TTL_MINUTES = 10


class RegistrationRejected(Exception):
    """A registration that cannot go through. Raised inside the transaction so it rolls back.

    Một lệnh đăng ký không thể thực hiện. Được ném ra bên trong transaction để transaction tự
    động bị hủy bỏ.
    """


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="tim_kiem_quy_che",
        description=(
            "Tim trong quy che dao tao va chuong trinh dao tao chinh thuc cua nha truong de "
            "tra loi cac cau hoi ve dieu kien tot nghiep, thang diem, canh bao hoc vu, tran "
            "tin chi, mon tien quyet, hoc phi, quy trinh dang ky. Bat buoc dung tool nay truoc "
            "khi tra loi bat ky cau hoi nao ve quy dinh cua nha truong."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "cau_hoi": types.Schema(
                    type=types.Type.STRING,
                    description="Cau hoi hoac tu khoa can tra cuu trong tai lieu.",
                )
            },
            required=["cau_hoi"],
        ),
    ),
    types.FunctionDeclaration(
        name="tim_lop_hoc_phan",
        description=(
            "Liet ke cac lop hoc phan dang mo cua mot hoc phan trong hoc ky hien tai, kem si "
            "so, lich hoc, giang vien va danh sach mon tien quyet. Dung tool nay de lay ma lop "
            "truoc khi dang ky."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "ma_mon": types.Schema(
                    type=types.Type.STRING,
                    description="Ma hoc phan, vi du INT3401. Neu bo trong thi liet ke tat ca lop dang mo.",
                )
            },
        ),
    ),
    types.FunctionDeclaration(
        name="tra_cuu_bang_diem",
        description=(
            "Tra cuu bang diem cac hoc phan sinh vien da hoc, kem diem so, diem chu va ket qua "
            "dat hay khong dat."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "hoc_ky": types.Schema(
                    type=types.Type.STRING,
                    description="Loc theo hoc ky, vi du 2024.1. Bo trong de lay toan bo bang diem.",
                )
            },
        ),
    ),
    types.FunctionDeclaration(
        name="tra_cuu_tien_do_hoc_tap",
        description=(
            "Tra cuu tong quan tien do hoc tap cua sinh vien: diem trung binh tich luy, so tin "
            "chi da tich luy, tinh trang hoc vu, tran tin chi duoc phep dang ky, so tin chi da "
            "dang ky trong hoc ky nay, va danh sach hoc phan bat buoc con thieu."
        ),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
    types.FunctionDeclaration(
        name="tinh_gpa_du_kien",
        description=(
            "Tinh diem trung binh chung tich luy du kien neu sinh vien dat cac muc diem gia "
            "dinh o mot so hoc phan. Dung de tra loi cau hoi dang 'neu em duoc 8 diem mon nay "
            "thi GPA cua em thanh bao nhieu'."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "du_kien": types.Schema(
                    type=types.Type.ARRAY,
                    description="Danh sach hoc phan kem muc diem gia dinh tren thang diem 10.",
                    items=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "ma_mon": types.Schema(
                                type=types.Type.STRING, description="Ma hoc phan, vi du INT3401."
                            ),
                            "diem": types.Schema(
                                type=types.Type.NUMBER,
                                description="Diem gia dinh tren thang diem 10.",
                            ),
                        },
                        required=["ma_mon", "diem"],
                    ),
                )
            },
            required=["du_kien"],
        ),
    ),
    types.FunctionDeclaration(
        name="dang_ky_hoc_phan",
        description=(
            "Buoc 1 cua dang ky hoc phan: ghi nhan nguyen vong dang ky va tra ve ma phieu. "
            "Tool nay KHONG ghi danh sinh vien vao lop va KHONG lam thay doi si so. Sau khi goi "
            "tool nay, phai doc lai cho sinh vien: ma hoc phan, ten hoc phan, so tin chi, nhom "
            "lop, giang vien, lich hoc, phong hoc, kem ma phieu, roi hoi sinh vien co xac nhan "
            "hay khong, va dung lai cho sinh vien tra loi."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "ma_lop": types.Schema(
                    type=types.Type.INTEGER,
                    description="Ma lop hoc phan, lay tu ket qua cua tool tim_lop_hoc_phan.",
                )
            },
            required=["ma_lop"],
        ),
    ),
    types.FunctionDeclaration(
        name="xac_nhan_dang_ky",
        description=(
            "Buoc 2 cua dang ky hoc phan: thuc hien ghi danh theo phieu da tao o buoc 1. Day la "
            "hanh dong lam thay doi du lieu. CHI duoc goi khi sinh vien da tra loi dong y trong "
            "mot tin nhan RIENG sau khi nghe doc lai thong tin lop. Tuyet doi khong goi tool nay "
            "trong cung tin nhan da tao ra phieu dang ky."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "ma_phieu": types.Schema(
                    type=types.Type.STRING,
                    description="Ma phieu do tool dang_ky_hoc_phan tra ve o buoc 1.",
                )
            },
            required=["ma_phieu"],
        ),
    ),
]

GEMINI_TOOLS = [types.Tool(function_declarations=TOOL_DECLARATIONS)]


# Loading the turn context
# Nạp dữ liệu cho lượt hiện tại
#
# Everything the guardrail is allowed to trust is read here, before the model runs. Reading it
# up front is what lets guardrail.py stay a pure function of its inputs.
# Mọi thứ guardrail được phép tin đều được đọc ở đây, trước khi model chạy. Chính việc đọc sẵn
# từ đầu là thứ cho phép guardrail.py vẫn là một hàm thuần túy của đầu vào.


def load_student(student_id: str) -> dict | None:
    """Read one student's whole record by id, or None if there is no such student.

    Đọc toàn bộ hồ sơ tổng quan của một sinh viên theo mã, hoặc None nếu không có.

    Chỉ SELECT đúng các cột cần dùng (không SELECT *) để không kéo về dữ liệu thừa. Không
    cần JOIN vì mọi thông tin tổng quan của sinh viên đều nằm ngay trong bảng students.
    fetchone() trả về dòng đầu tiên - ở đây tối đa là một dòng vì student_id là khóa chính -
    hoặc None nếu không khớp dòng nào.
    """
    with get_connection() as conn:
        return conn.execute(
            """
            SELECT student_id, full_name, major, cohort, gpa, credits_earned, academic_status
            FROM students WHERE student_id = %s
            """,
            (student_id,),
        ).fetchone()


# Each of these comes in two forms on purpose. The public one opens its own connection and is
# what the agent calls before the model runs. The private one takes a connection it is given, and
# is what the confirmation transaction calls - because a fact re-read on a *different* connection
# would be read outside the transaction, would not see the locks that transaction holds, and so
# would be exactly the stale fact the lock was taken to avoid.
# Mỗi hàm dưới đây có hai dạng, và đó là cố ý. Dạng công khai tự mở kết nối riêng, và là dạng mà
# agent gọi trước khi model chạy. Dạng riêng tư thì nhận một kết nối được đưa cho, và là dạng mà
# transaction xác nhận gọi - bởi một sự thật đọc lại trên một kết nối KHÁC thì sẽ được đọc bên
# ngoài transaction, sẽ không nhìn thấy các khóa mà transaction đó đang giữ, và vì vậy sẽ đúng là
# cái sự thật cũ kỹ mà cái khóa sinh ra để tránh.


def load_passed_courses(student_id: str) -> frozenset[str]:
    """The courses this student has actually passed, straight from the grade table.

    Các học phần sinh viên này thực sự đã đạt, lấy thẳng từ bảng điểm.

    This is the only answer to "have I done the prerequisite" that the service accepts. What
    the student says, and what the model believes them, do not enter into it.
    Đây là câu trả lời duy nhất cho câu hỏi "em đã học môn tiên quyết chưa" mà dịch vụ chấp
    nhận. Sinh viên nói gì, và model có tin theo hay không, không liên quan gì ở đây.
    """
    with get_connection() as conn:
        return _passed_courses(conn, student_id)


def _passed_courses(conn, student_id: str) -> frozenset[str]:
    # Lấy mã các môn sinh viên đã ĐẠT (cột passed = true trong bảng grades). Trả về một
    # frozenset để guardrail so sánh nhanh "tập môn đã đạt" với "tập môn tiên quyết" bằng
    # phép trừ tập hợp (xem check_registration_rules trong guardrail.py).
    rows = conn.execute(
        "SELECT course_code FROM grades WHERE student_id = %s AND passed",
        (student_id,),
    ).fetchall()
    return frozenset(row["course_code"] for row in rows)


def load_registered(student_id: str, semester: str) -> tuple[RegisteredClass, ...]:
    with get_connection() as conn:
        return _registered(conn, student_id, semester)


def _registered(conn, student_id: str, semester: str) -> tuple[RegisteredClass, ...]:
    # Danh sách các lớp sinh viên đã đăng ký trong học kỳ. Ghép ba bảng lại với nhau:
    #   enrollments (e)     -> sinh viên này đã ghi danh vào những lớp nào
    #   class_sections (s)  -> lấy lịch học của từng lớp (thứ, tiết bắt đầu, tiết kết thúc)
    #   courses (c)         -> lấy tên môn và số tín chỉ
    # Lọc theo đúng sinh viên và đúng học kỳ, sắp xếp theo thứ rồi theo tiết cho dễ đọc.
    # Guardrail dùng kết quả này để kiểm tra trùng lịch và cộng dồn tổng tín chỉ đã đăng ký.
    rows = conn.execute(
        """
        SELECT s.id, e.course_code, c.course_name, c.credits,
               s.day_of_week, s.start_period, s.end_period
        FROM enrollments e
        JOIN class_sections s ON s.id = e.class_section_id
        JOIN courses c ON c.course_code = e.course_code
        WHERE e.student_id = %s AND e.semester = %s
        ORDER BY s.day_of_week, s.start_period
        """,
        (student_id, semester),
    ).fetchall()

    return tuple(
        RegisteredClass(
            class_section_id=row["id"],
            course_code=row["course_code"],
            course_name=row["course_name"],
            credits=row["credits"],
            day_of_week=row["day_of_week"],
            start_period=row["start_period"],
            end_period=row["end_period"],
        )
        for row in rows
    )


def is_registration_open(semester: str) -> bool:
    """Whether registration is open right now, as decided by the database clock.

    Đăng ký học phần có đang mở hay không, do đồng hồ của database quyết định.

    The comparison is made by PostgreSQL rather than by the application so a clock skew
    between the two cannot reopen a window that has already closed.
    Phép so sánh do PostgreSQL thực hiện chứ không phải ứng dụng, để lệch đồng hồ giữa hai bên
    không thể mở lại một đợt đăng ký vốn đã đóng.
    """
    with get_connection() as conn:
        return _registration_open(conn, semester)


def _registration_open(conn, semester: str) -> bool:
    # Hỏi thẳng PostgreSQL xem thời điểm "bây giờ" (now()) có nằm trong khoảng mở đăng ký của
    # học kỳ hay không. Phép so sánh đặt ở phía database, không phải ở phía ứng dụng, để nếu
    # đồng hồ hai bên lệch nhau thì cũng không thể mở lại một đợt đăng ký đã đóng.
    # row có thể là None (không có dòng nào cho học kỳ này) -> khi đó coi như đăng ký không mở.
    row = conn.execute(
        """
        SELECT (now() BETWEEN opens_at AND closes_at) AS is_open
        FROM registration_windows WHERE semester = %s
        """,
        (semester,),
    ).fetchone()
    return bool(row and row["is_open"])


def load_class_section(section_id: int, semester: str) -> ClassSection | None:
    """Load one class section and everything the rules need to judge it.

    Nạp một lớp học phần kèm mọi thứ mà các quy tắc cần để phân xử nó.

    Đọc bằng HAI truy vấn thay vì một:
      1. Ghép class_sections với courses để lấy lịch học, sĩ số, tên môn, số tín chỉ.
         Nếu không tìm thấy lớp (sai mã hoặc sai học kỳ) thì trả về None ngay.
      2. Lấy danh sách môn tiên quyết của môn này từ bảng prerequisites.
    Tách làm hai là để tránh JOIN nhân bản dòng: một môn có N môn tiên quyết sẽ làm truy vấn
    ghép trả về N dòng trùng lặp thông tin lớp. Ở đây thông tin lớp chỉ cần đúng một dòng.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT s.id, s.course_code, c.course_name, s.section_no, c.credits,
                   s.capacity, s.enrolled, s.day_of_week, s.start_period, s.end_period
            FROM class_sections s
            JOIN courses c ON c.course_code = s.course_code
            WHERE s.id = %s AND s.semester = %s
            """,
            (section_id, semester),
        ).fetchone()
        if row is None:
            return None

        # Danh sách mã môn tiên quyết của môn này, đọc riêng rồi gom thành frozenset bên dưới.
        prereqs = conn.execute(
            "SELECT prereq_code FROM prerequisites WHERE course_code = %s",
            (row["course_code"],),
        ).fetchall()

    return ClassSection(
        id=row["id"],
        course_code=row["course_code"],
        course_name=row["course_name"],
        section_no=row["section_no"],
        credits=row["credits"],
        capacity=row["capacity"],
        enrolled=row["enrolled"],
        day_of_week=row["day_of_week"],
        start_period=row["start_period"],
        end_period=row["end_period"],
        prereq_codes=frozenset(item["prereq_code"] for item in prereqs),
    )


def load_pending_registration(slip_id: str) -> PendingRegistration | None:
    """Read a prepared registration, letting PostgreSQL decide whether it has expired.

    Đọc một phiếu đăng ký đã chuẩn bị, để PostgreSQL tự quyết định nó hết hạn hay chưa.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, session_id, student_id, created_turn_id, class_section_id, status,
                   (expires_at <= now()) AS expired
            FROM pending_registrations
            WHERE id = %s
            """,
            (slip_id,),
        ).fetchone()

    if row is None:
        return None
    return PendingRegistration(**row)


class ToolExecutor:
    def __init__(self, retriever: Retriever, settings: Settings) -> None:
        self._retriever = retriever
        self._settings = settings
        self._retrieval_top_k = settings.retrieval_top_k
        self._semester = settings.current_semester

    def execute(self, tool_name: str, arguments: dict, context: TurnContext) -> dict:
        """Route a tool name to its handler and run it. Only called after the guardrail allows it.

        Điều hướng một tên tool tới đúng handler của nó rồi chạy. Chỉ được gọi sau khi guardrail
        đã cho phép (xem loop.py:_handle_tool_call).

        Dùng một bảng tra cứu (dict tên tool -> hàm xử lý) thay vì một chuỗi if/elif dài. Mọi
        hàm xử lý đều nhận cùng bộ tham số (arguments, context) và trả về một dict sẽ được nạp
        ngược lại cho model dưới dạng kết quả tool.
        """
        handlers = {
            "tim_kiem_quy_che": self._tim_kiem_quy_che,
            "tim_lop_hoc_phan": self._tim_lop_hoc_phan,
            "tra_cuu_bang_diem": self._tra_cuu_bang_diem,
            "tra_cuu_tien_do_hoc_tap": self._tra_cuu_tien_do_hoc_tap,
            "tinh_gpa_du_kien": self._tinh_gpa_du_kien,
            "dang_ky_hoc_phan": self._dang_ky_hoc_phan,
            "xac_nhan_dang_ky": self._xac_nhan_dang_ky,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return {"loi": f"Tool khong ton tai: {tool_name}"}
        return handler(arguments, context)

    def _tim_kiem_quy_che(self, arguments: dict, context: TurnContext) -> dict:
        """RAG: tìm các đoạn tài liệu liên quan nhất tới câu hỏi, kèm nguồn trích dẫn.

        Đây là bước "R" (Retrieval) của RAG. Lấy câu hỏi model đưa ra, gọi Retriever để tìm
        top_k đoạn giống nhất trong kho tri thức, rồi trả về nội dung kèm tiêu đề và nguồn.
        Nếu không tìm thấy gì, trả về ghi chú báo model phải nói thẳng là không có thông tin,
        tuyệt đối không được bịa. Chính phần "nguồn" giúp model trích dẫn lại cho sinh viên.
        """
        query = str(arguments.get("cau_hoi", "")).strip()
        if not query:
            return {"loi": "Thieu cau hoi can tra cuu."}

        results = self._retriever.search(query, top_k=self._retrieval_top_k)
        if not results:
            return {
                "ket_qua": [],
                "ghi_chu": "Khong tim thay tai lieu lien quan. Hay noi ro la khong co thong tin.",
            }

        return {
            "ket_qua": [
                {"noi_dung": r.content, "tieu_de": r.title, "nguon": r.source} for r in results
            ]
        }

    def _tim_lop_hoc_phan(self, arguments: dict, context: TurnContext) -> dict:
        """List open class sections for a course (or all courses), with schedule and prerequisites.

        Liệt kê các lớp học phần đang mở của một môn (hoặc tất cả môn), kèm lịch học và môn tiên
        quyết. Model dùng tool này để lấy ma_lop trước khi gọi dang_ky_hoc_phan.

        Chuẩn hóa mã môn về chữ HOA để "int3401" và "INT3401" đều khớp. Nếu có ma_mon thì lọc
        theo môn đó, nếu bỏ trống thì liệt kê mọi lớp đang mở trong học kỳ. Sau khi lấy danh
        sách lớp, đọc thêm môn tiên quyết của tất cả các môn trong kết quả bằng MỘT truy vấn
        (dùng ANY(%s)) rồi gom lại theo môn, tránh gọi database lặp đi lặp lại cho từng môn.
        """
        course_code = str(arguments.get("ma_mon") or "").strip().upper()

        with get_connection() as conn:
            if course_code:
                # Nhánh 1: chỉ lấy các lớp của đúng một môn. JOIN courses để kèm tên môn và tín chỉ.
                rows = conn.execute(
                    """
                    SELECT s.id, s.course_code, c.course_name, c.credits, s.section_no,
                           s.lecturer, s.capacity, s.enrolled, s.day_of_week,
                           s.start_period, s.end_period, s.room
                    FROM class_sections s
                    JOIN courses c ON c.course_code = s.course_code
                    WHERE s.semester = %s AND s.course_code = %s
                    ORDER BY s.section_no
                    """,
                    (self._semester, course_code),
                ).fetchall()
            else:
                # Nhánh 2: không lọc theo môn, lấy hết mọi lớp đang mở trong học kỳ.
                rows = conn.execute(
                    """
                    SELECT s.id, s.course_code, c.course_name, c.credits, s.section_no,
                           s.lecturer, s.capacity, s.enrolled, s.day_of_week,
                           s.start_period, s.end_period, s.room
                    FROM class_sections s
                    JOIN courses c ON c.course_code = s.course_code
                    WHERE s.semester = %s
                    ORDER BY s.course_code, s.section_no
                    """,
                    (self._semester,),
                ).fetchall()

            if not rows:
                return {
                    "loi": (
                        f"Khong co lop hoc phan nao dang mo cho hoc phan {course_code} "
                        f"trong hoc ky {self._semester}."
                        if course_code
                        else f"Khong co lop hoc phan nao dang mo trong hoc ky {self._semester}."
                    )
                }

            # Lấy môn tiên quyết cho tất cả các môn có trong kết quả chỉ bằng một truy vấn.
            # ANY(%s) nhận một danh sách mã môn và khớp bất kỳ mã nào trong danh sách đó, thay
            # cho việc chạy một truy vấn riêng cho từng môn (tránh lỗi "N+1 query").
            prereq_rows = conn.execute(
                """
                SELECT course_code, prereq_code FROM prerequisites
                WHERE course_code = ANY(%s)
                """,
                ([row["course_code"] for row in rows],),
            ).fetchall()

        # Gom các dòng (môn, môn_tiên_quyết) thành dict: môn -> [danh sách môn tiên quyết].
        # setdefault tạo sẵn list rỗng cho lần gặp môn đó đầu tiên rồi mới append vào.
        prereqs: dict[str, list[str]] = {}
        for row in prereq_rows:
            prereqs.setdefault(row["course_code"], []).append(row["prereq_code"])

        return {
            "hoc_ky": self._semester,
            "cac_lop": [
                {
                    "ma_lop": row["id"],
                    "ma_mon": row["course_code"],
                    "ten_mon": row["course_name"],
                    "so_tin_chi": row["credits"],
                    "nhom": row["section_no"],
                    "giang_vien": row["lecturer"],
                    "lich_hoc": _schedule_text(
                        row["day_of_week"], row["start_period"], row["end_period"]
                    ),
                    "phong": row["room"],
                    "si_so": f"{row['enrolled']}/{row['capacity']}",
                    "con_cho": max(row["capacity"] - row["enrolled"], 0),
                    "mon_tien_quyet": sorted(prereqs.get(row["course_code"], [])),
                }
                for row in rows
            ],
        }

    def _tra_cuu_bang_diem(self, arguments: dict, context: TurnContext) -> dict:
        """Look up the student's transcript, optionally filtered to one semester.

        Tra cứu bảng điểm của sinh viên, có thể lọc theo một học kỳ.

        Chú ý: mã sinh viên lấy từ context.student_id (từ token đã xác thực), KHÔNG lấy từ
        tham số của model - nên model không thể đọc bảng điểm của người khác. JOIN grades với
        courses để mỗi dòng điểm có kèm tên môn và số tín chỉ. Điểm chữ (letter_grade) và kết
        quả đạt/không đạt được tính ở tầng Python bằng chung một module grading.py, để con số
        trợ lý đọc ra luôn khớp với quy chế.
        """
        semester = str(arguments.get("hoc_ky") or "").strip()

        with get_connection() as conn:
            if semester:
                rows = conn.execute(
                    """
                    SELECT g.course_code, c.course_name, c.credits, g.semester, g.score, g.passed
                    FROM grades g
                    JOIN courses c ON c.course_code = g.course_code
                    WHERE g.student_id = %s AND g.semester = %s
                    ORDER BY g.semester, g.course_code
                    """,
                    (context.student_id, semester),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT g.course_code, c.course_name, c.credits, g.semester, g.score, g.passed
                    FROM grades g
                    JOIN courses c ON c.course_code = g.course_code
                    WHERE g.student_id = %s
                    ORDER BY g.semester, g.course_code
                    """,
                    (context.student_id,),
                ).fetchall()

        if not rows:
            return {"loi": "Khong co du lieu diem cho sinh vien nay."}

        return {
            "bang_diem": [
                {
                    "ma_mon": row["course_code"],
                    "ten_mon": row["course_name"],
                    "so_tin_chi": row["credits"],
                    "hoc_ky": row["semester"],
                    "diem_he_10": float(row["score"]),
                    "diem_chu": letter_grade(row["score"]),
                    "ket_qua": "Dat" if row["passed"] else "Khong dat",
                }
                for row in rows
            ],
            "ghi_chu": f"Hoc phan duoc coi la dat khi diem tu {PASS_MARK} tro len.",
        }

    def _tra_cuu_tien_do_hoc_tap(self, arguments: dict, context: TurnContext) -> dict:
        """Overview of the student's progress: GPA, credits, status, ceiling, courses still owed.

        Tổng quan tiến độ học tập: GPA, tín chỉ, tình trạng học vụ, trần tín chỉ, và các môn
        bắt buộc còn thiếu. Gộp dữ liệu từ ba nguồn: hồ sơ sinh viên (load_student), trần tín
        chỉ và các lớp đã đăng ký (có sẵn trong context), và truy vấn "môn bắt buộc còn thiếu"
        bên dưới.
        """
        student = load_student(context.student_id)
        if student is None:
            return {"loi": "Khong tim thay sinh vien."}

        with get_connection() as conn:
            # Các môn BẮT BUỘC (is_required) mà sinh viên CHƯA đạt.
            # Cách đọc: lấy mọi môn bắt buộc, rồi loại bỏ những môn đã nằm trong tập "các môn
            # sinh viên đã đạt" - chính là truy vấn con NOT IN (SELECT ... WHERE passed).
            # Kết quả là danh sách môn bắt buộc còn nợ lại, dùng để nhắc sinh viên cần học thêm.
            missing = conn.execute(
                """
                SELECT c.course_code, c.course_name, c.credits
                FROM courses c
                WHERE c.is_required
                  AND c.course_code NOT IN (
                      SELECT course_code FROM grades
                      WHERE student_id = %s AND passed
                  )
                ORDER BY c.course_code
                """,
                (context.student_id,),
            ).fetchall()

        return {
            "ma_sinh_vien": student["student_id"],
            "ho_ten": student["full_name"],
            "nganh": student["major"],
            "gpa_tich_luy": float(student["gpa"]),
            "tin_chi_tich_luy": student["credits_earned"],
            "tinh_trang_hoc_vu": student["academic_status"],
            "tran_tin_chi_hoc_ky": context.max_credits,
            "tin_chi_da_dang_ky_hoc_ky_nay": context.registered_credits,
            "tin_chi_con_duoc_dang_ky": max(
                context.max_credits - context.registered_credits, 0
            ),
            "cac_lop_da_dang_ky": [
                {
                    "ma_mon": item.course_code,
                    "ten_mon": item.course_name,
                    "so_tin_chi": item.credits,
                    "lich_hoc": _schedule_text(
                        item.day_of_week, item.start_period, item.end_period
                    ),
                }
                for item in context.registered
            ],
            "hoc_phan_bat_buoc_con_thieu": [
                {
                    "ma_mon": row["course_code"],
                    "ten_mon": row["course_name"],
                    "so_tin_chi": row["credits"],
                }
                for row in missing
            ],
        }

    def _tinh_gpa_du_kien(self, arguments: dict, context: TurnContext) -> dict:
        """Recompute the GPA as if the student scored the given marks in the given courses.

        Tính lại GPA như thể sinh viên đạt các mức điểm giả định ở các học phần đã nêu.

        A repeated course replaces the earlier attempt rather than being added alongside it,
        which is what the regulation says about retaking a course. Adding it twice would let a
        student "improve" their GPA by simply retaking a course they had already passed well.
        Một học phần học lại sẽ thay thế lần học trước chứ không được cộng thêm bên cạnh, đúng
        như quy chế quy định về học lại. Nếu cộng cả hai lần, sinh viên có thể "cải thiện" GPA
        chỉ bằng cách học lại một môn vốn đã đạt điểm cao.
        """
        assumed = arguments.get("du_kien") or []
        if not isinstance(assumed, list) or not assumed:
            return {"loi": "Thieu danh sach hoc phan va diem du kien."}

        with get_connection() as conn:
            current = conn.execute(
                """
                SELECT g.course_code, c.credits, g.score
                FROM grades g
                JOIN courses c ON c.course_code = g.course_code
                WHERE g.student_id = %s
                """,
                (context.student_id,),
            ).fetchall()
            credit_rows = conn.execute("SELECT course_code, credits FROM courses").fetchall()

        # credits_of: mã môn -> số tín chỉ, dùng để tra nhanh trọng số khi tính GPA.
        # scores: mã môn -> điểm hiện tại. Khởi tạo từ bảng điểm thật. Ở vòng lặp bên dưới,
        # gán scores[code] = score sẽ GHI ĐÈ điểm cũ nếu môn đó đã có - đúng là cách quy chế
        # xử lý học lại (lấy điểm lần sau, không cộng thêm lần trước).
        credits_of = {row["course_code"]: row["credits"] for row in credit_rows}
        scores: dict[str, Decimal] = {row["course_code"]: row["score"] for row in current}

        applied = []
        for item in assumed:
            if not isinstance(item, dict):
                continue
            code = str(item.get("ma_mon", "")).strip().upper()
            raw_score = item.get("diem")
            if code not in credits_of:
                return {"loi": f"Khong tim thay hoc phan {code} trong chuong trinh dao tao."}
            if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
                return {"loi": f"Diem du kien cua hoc phan {code} khong hop le."}
            score = Decimal(str(raw_score))
            if score < 0 or score > 10:
                return {"loi": f"Diem du kien cua hoc phan {code} phai nam trong khoang 0 den 10."}

            scores[code] = score
            applied.append(
                {
                    "ma_mon": code,
                    "diem_du_kien": float(score),
                    "diem_chu": letter_grade(score),
                    "diem_he_4": float(grade_point(score)),
                    "ket_qua": "Dat" if is_passed(score) else "Khong dat",
                }
            )

        if not applied:
            return {"loi": "Thieu danh sach hoc phan va diem du kien."}

        # Hai tập (điểm, tín chỉ) để đưa vào compute_gpa: một tập theo điểm hiện tại, một tập
        # theo điểm sau khi đã áp các điểm giả định (scores đã bao gồm các điểm ghi đè ở trên).
        current_entries = [(row["score"], credits_of[row["course_code"]]) for row in current]
        projected_entries = [(score, credits_of[code]) for code, score in scores.items()]

        return {
            "gpa_hien_tai": float(compute_gpa(current_entries)),
            "gpa_du_kien": float(compute_gpa(projected_entries)),
            "tin_chi_tich_luy_du_kien": sum(
                credits_of[code] for code, score in scores.items() if is_passed(score)
            ),
            "cac_mon_gia_dinh": applied,
            "ghi_chu": (
                "GPA du kien tinh theo trung binh co trong so tren thang diem 4, co tinh ca cac "
                "hoc phan khong dat voi 0 diem. Hoc phan hoc lai thay the diem cua lan hoc truoc."
            ),
        }

    def _dang_ky_hoc_phan(self, arguments: dict, context: TurnContext) -> dict:
        """Step one: write the request down and hand back a slip. Nobody is enrolled here.

        Bước một: ghi lại nguyện vọng đăng ký và trả về một mã phiếu. Chưa ai được ghi danh ở
        bước này.
        """
        target = context.target
        if target is None:
            return {"loi": "Khong tim thay lop hoc phan nay."}

        with get_connection() as conn:
            section = conn.execute(
                "SELECT lecturer, room FROM class_sections WHERE id = %s", (target.id,)
            ).fetchone()

            # The code is unguessable on purpose: it is the token the student's next message is
            # matched against, so it must not be possible to guess someone else's pending slip
            # into existence.
            # Mã phiếu cố ý khó đoán: nó là mã mà tin nhắn tiếp theo của sinh viên sẽ đối chiếu
            # vào, nên không được phép đoán mò ra phiếu chờ của người khác.
            slip_id = f"DK{secrets.token_hex(3).upper()}"

            conn.execute(
                f"""
                INSERT INTO pending_registrations (
                    id, session_id, student_id, created_turn_id, class_section_id, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, now() + interval '{CONFIRM_TTL_MINUTES} minutes')
                """,
                (
                    slip_id,
                    context.session_id,
                    context.student_id,
                    context.turn_id,
                    target.id,
                ),
            )

        return {
            "trang_thai": "cho_sinh_vien_xac_nhan",
            "ma_phieu": slip_id,
            "ma_lop": target.id,
            "ma_mon": target.course_code,
            "ten_mon": target.course_name,
            "so_tin_chi": target.credits,
            "nhom": target.section_no,
            "giang_vien": section["lecturer"],
            "lich_hoc": target.schedule_text(),
            "phong": section["room"],
            "si_so": f"{target.enrolled}/{target.capacity}",
            "tong_tin_chi_sau_khi_dang_ky": context.registered_credits + target.credits,
            "tran_tin_chi": context.max_credits,
            "han_xac_nhan_phut": CONFIRM_TTL_MINUTES,
            "huong_dan": (
                "Chua ghi danh. Hay doc lai thong tin lop tren cho sinh vien, kem ma phieu, roi "
                "hoi sinh vien co xac nhan khong va dung lai cho sinh vien tra loi."
            ),
        }

    def _xac_nhan_dang_ky(self, arguments: dict, context: TurnContext) -> dict:
        """Step two: carry out a registration the student confirmed. The class fills up here.

        Bước hai: thực hiện lệnh đăng ký sinh viên đã xác nhận. Lớp đầy lên ở đây.

        The guardrail has already approved this call, but nothing here trusts that: the slip is
        claimed again under lock and the seat is counted again under lock. The guardrail read
        the seat count a moment ago, outside any transaction, and by now another student may
        have taken the last seat.
        Guardrail đã duyệt lời gọi này, nhưng ở đây không tin vào điều đó: phiếu được giành lại
        một lần nữa trong trạng thái khóa, và chỗ ngồi cũng được đếm lại trong trạng thái khóa.
        Guardrail đọc sĩ số từ một lúc trước, bên ngoài mọi transaction, và đến giờ này một sinh
        viên khác hoàn toàn có thể đã lấy mất chỗ cuối cùng.
        """
        slip_id = str(arguments.get("ma_phieu", "")).strip()

        try:
            with get_connection() as conn:
                with conn.transaction():
                    return execute_registration(conn, slip_id, self._settings)
        except RegistrationRejected as rejection:
            # Raised inside the transaction, so PostgreSQL has rolled everything back, including
            # the slip we had marked as executed. It goes back to waiting.
            # Được ném ra bên trong transaction, nên PostgreSQL đã hủy bỏ toàn bộ, kể cả dòng
            # phiếu vừa bị đánh dấu là đã thực hiện. Nó quay lại trạng thái chờ.
            return {"loi": str(rejection)}


def execute_registration(conn, slip_id: str, settings: Settings) -> dict:
    """Enrol the student named on the slip, or refuse. Runs inside a transaction.

    Ghi danh sinh viên ghi trên phiếu, hoặc từ chối. Chạy bên trong một transaction.

    The guardrail has already approved this call, and none of that approval is trusted here. It
    was made against facts read at the start of the turn, on another connection, outside every
    transaction. Between then and now another confirmation may have gone through - for this same
    student, or for this same class - and every one of those facts may have moved.

    So the six rules are run again, here, over facts re-read under lock. This is the run that
    counts; the earlier one only existed to tell the student early.

    Guardrail đã duyệt lời gọi này, và ở đây không tin vào sự phê duyệt đó chút nào. Nó được đưa
    ra dựa trên những sự thật đọc từ đầu lượt, trên một kết nối khác, bên ngoài mọi transaction.
    Từ lúc đó đến giờ, một lệnh xác nhận khác hoàn toàn có thể đã đi qua - của chính sinh viên
    này, hoặc vào chính lớp này - và mọi sự thật kia đều có thể đã đổi.

    Vì vậy sáu quy tắc được chạy lại, tại đây, trên những sự thật đọc lại dưới khóa. Đây mới là
    lần chạy có hiệu lực; lần trước đó chỉ tồn tại để báo sớm cho sinh viên.

    Exposed at module level rather than kept as a method so the concurrency test can fire it
    from many threads at once without standing up an agent.
    Hàm này đặt ở cấp module thay vì là một method, để bài test tranh chấp đồng thời có thể gọi
    thẳng nó từ nhiều luồng cùng lúc mà không cần dựng lên cả một agent.
    """
    # Claim the slip by flipping its status, and only proceed if this statement is the one that
    # flipped it. A second confirmation of the same code - whether from a retried request or
    # from the model calling the tool twice - updates no rows and enrols nobody.
    # Giành lấy phiếu bằng cách lật trạng thái của nó, và chỉ đi tiếp nếu chính câu lệnh này là
    # câu đã lật được. Một lần xác nhận thứ hai trên cùng mã phiếu, dù đến từ request bị gửi lại
    # hay từ việc model gọi tool hai lần, sẽ không cập nhật dòng nào và không ghi danh ai cả.
    claimed = conn.execute(
        """
        UPDATE pending_registrations
        SET status = 'da_thuc_hien'
        WHERE id = %s AND status = 'cho_xac_nhan' AND expires_at > now()
        RETURNING student_id, session_id, created_turn_id, class_section_id
        """,
        (slip_id,),
    ).fetchone()

    if claimed is None:
        raise RegistrationRejected(
            f"Phieu {slip_id} khong con o trang thai cho xac nhan (da thuc hien hoac da het han)."
        )

    student_id = claimed["student_id"]
    section_id = claimed["class_section_id"]

    # Lock the STUDENT row, before anything else about this student is read.
    #
    # This is the lock that closes the hole the class lock never covered. The class lock protects
    # the seat count, which is contested between *different* students. But the credit ceiling and
    # the timetable clash are properties of *one* student's set of enrolments, and they are
    # contested when that same student confirms two slips at once - two browser tabs, a retried
    # request, a model that fires both confirmations in one response. Both would read the same
    # stale list of enrolments, both would find room under the ceiling, and both would go through,
    # leaving the student in two classes that clash or past the credit cap.
    #
    # Holding this row makes every confirmation for this student queue behind the one in front of
    # it, so the enrolments read below are the enrolments as of now, not as of the start of the
    # turn.
    #
    # Khóa dòng SINH VIÊN, trước khi đọc bất cứ thứ gì khác về sinh viên này.
    #
    # Đây là cái khóa bịt lại lỗ hổng mà khóa lớp không bao giờ chạm tới. Khóa lớp bảo vệ sĩ số,
    # vốn bị tranh chấp giữa NHỮNG sinh viên khác nhau. Nhưng trần tín chỉ và việc trùng lịch lại
    # là thuộc tính của tập các lớp của MỘT sinh viên, và chúng bị tranh chấp khi chính sinh viên
    # đó xác nhận hai phiếu cùng lúc - hai tab trình duyệt, một request bị gửi lại, hay một model
    # gọi cả hai lệnh xác nhận trong cùng một câu trả lời. Cả hai đều sẽ đọc cùng một danh sách lớp
    # cũ kỹ, cả hai đều thấy còn chỗ dưới trần, và cả hai đều đi qua, để lại sinh viên trong hai
    # lớp trùng lịch nhau hoặc vượt trần tín chỉ.
    #
    # Giữ dòng này khiến mọi lệnh xác nhận của sinh viên này phải xếp hàng sau lệnh đang chạy, nên
    # danh sách lớp đọc ở dưới là danh sách tại thời điểm này, không phải từ đầu lượt.
    #
    # The order matters and is fixed everywhere: student first, then class. Two transactions that
    # took them in opposite orders would sooner or later wait on each other for ever.
    # Thứ tự khóa là quan trọng và được cố định ở khắp nơi: sinh viên trước, lớp sau. Hai
    # transaction lấy hai khóa này theo hai thứ tự ngược nhau thì sớm muộn cũng sẽ chờ nhau mãi mãi.
    student = conn.execute(
        "SELECT student_id, academic_status FROM students WHERE student_id = %s FOR UPDATE",
        (student_id,),
    ).fetchone()

    if student is None:
        raise RegistrationRejected("Khong tim thay sinh vien cua phieu dang ky nay.")

    # Lock the class row. Every other confirmation for this same class now queues up behind this
    # statement, so the seat count read below cannot change under our feet between reading it
    # and acting on it.
    #
    # This lock is not what keeps the data correct - the CHECK constraint on class_sections
    # already does that, and it was measured doing so: with the lock removed, 19 of 20
    # simultaneous confirmations still failed, but they failed as a CheckViolation raised by
    # PostgreSQL. What the lock buys is the *quality of the refusal*. Without it, 19 students
    # get a raw constraint error that surfaces as "the tool crashed"; with it, they get
    # "the class is full, please pick another one". Correctness and a usable answer are two
    # different problems, and they are solved by two different lines of defence.
    #
    # Khóa dòng của lớp. Mọi lệnh xác nhận khác vào cùng lớp này từ giờ phải xếp hàng sau câu
    # lệnh này, nên sĩ số đọc ở dưới không thể đổi ngay dưới chân ta trong khoảng giữa lúc đọc
    # và lúc hành động.
    #
    # Cái khóa này không phải là thứ giữ cho dữ liệu đúng - ràng buộc CHECK trên class_sections
    # đã làm việc đó rồi, và điều này đã được đo thật: khi bỏ khóa đi, 19 trên 20 lệnh xác nhận
    # đồng thời vẫn thất bại, nhưng chúng thất bại dưới dạng CheckViolation do PostgreSQL ném
    # ra. Thứ mà cái khóa mua về là *chất lượng của lời từ chối*. Không có nó, 19 sinh viên nhận
    # một lỗi ràng buộc thô, lộ ra ngoài thành "tool bị lỗi"; có nó, họ nhận được câu "lớp đã đủ
    # sĩ số, em chọn lớp khác nhé". Dữ liệu đúng và câu trả lời đúng là hai bài toán khác nhau,
    # và chúng được giải bằng hai lớp phòng thủ khác nhau.
    section = conn.execute(
        """
        SELECT s.id, s.course_code, c.course_name, s.section_no, c.credits, s.semester,
               s.capacity, s.enrolled, s.day_of_week, s.start_period, s.end_period
        FROM class_sections s
        JOIN courses c ON c.course_code = s.course_code
        WHERE s.id = %s
        FOR UPDATE OF s
        """,
        (section_id,),
    ).fetchone()

    if section is None:
        raise RegistrationRejected("Khong tim thay lop hoc phan cua phieu dang ky nay.")

    semester = section["semester"]
    prereqs = conn.execute(
        "SELECT prereq_code FROM prerequisites WHERE course_code = %s",
        (section["course_code"],),
    ).fetchall()

    target = ClassSection(
        id=section["id"],
        course_code=section["course_code"],
        course_name=section["course_name"],
        section_no=section["section_no"],
        credits=section["credits"],
        capacity=section["capacity"],
        enrolled=section["enrolled"],
        day_of_week=section["day_of_week"],
        start_period=section["start_period"],
        end_period=section["end_period"],
        prereq_codes=frozenset(row["prereq_code"] for row in prereqs),
    )

    academic_status = student["academic_status"]

    # Rebuilt entirely from what this transaction can see, holding both locks. The very same pure
    # function the guardrail used is run again over it - the rules are not restated here, because
    # two copies of six rules would eventually disagree, and the copy that disagreed silently
    # would be the one guarding the write.
    # Dựng lại hoàn toàn từ những gì transaction này nhìn thấy, khi đang giữ cả hai khóa. Chính
    # đúng hàm thuần mà guardrail đã dùng sẽ được chạy lại trên nó - các quy tắc không được viết
    # lại ở đây, bởi hai bản sao của sáu quy tắc rồi sẽ có lúc lệch nhau, và bản sao lệch một cách
    # âm thầm sẽ đúng là bản đang canh lệnh ghi.
    fresh = TurnContext(
        student_id=student_id,
        session_id=claimed["session_id"],
        turn_id=claimed["created_turn_id"],
        semester=semester,
        academic_status=academic_status,
        max_credits=settings.max_credits_for(academic_status),
        registration_open=_registration_open(conn, semester),
        passed_courses=_passed_courses(conn, student_id),
        registered=_registered(conn, student_id, semester),
        target=target,
    )

    decision = check_registration_rules(target, fresh)
    if not decision.allowed:
        raise RegistrationRejected(decision.note or "Lenh dang ky bi tu choi.")

    # Both writes happen while still holding both locks: add the enrolment row, then bump the
    # seat count that the CHECK constraint watches.
    # Cả hai lệnh ghi diễn ra khi vẫn đang giữ cả hai khóa: thêm dòng ghi danh, rồi tăng sĩ số
    # mà ràng buộc CHECK đang canh chừng.
    conn.execute(
        """
        INSERT INTO enrollments (student_id, class_section_id, course_code, semester)
        VALUES (%s, %s, %s, %s)
        """,
        (student_id, section_id, section["course_code"], semester),
    )
    conn.execute(
        "UPDATE class_sections SET enrolled = enrolled + 1 WHERE id = %s",
        (section_id,),
    )

    updated = conn.execute(
        "SELECT enrolled, capacity FROM class_sections WHERE id = %s", (section_id,)
    ).fetchone()

    return {
        "trang_thai": "dang_ky_thanh_cong",
        "ma_phieu": slip_id,
        "ma_lop": section_id,
        "ma_mon": section["course_code"],
        "ten_mon": section["course_name"],
        "nhom": section["section_no"],
        "hoc_ky": semester,
        "si_so_sau_dang_ky": f"{updated['enrolled']}/{updated['capacity']}",
    }


def _schedule_text(day_of_week: int, start_period: int, end_period: int) -> str:
    """Format a class schedule into readable Vietnamese, e.g. "Thu 3, tiet 1-3".

    Định dạng lịch học thành chuỗi tiếng Việt dễ đọc, ví dụ "Thu 3, tiet 1-3".

    Quy ước day_of_week theo thời khóa biểu Việt Nam: 2 = thứ Hai, ..., 7 = thứ Bảy,
    8 = Chủ nhật (xem ràng buộc CHECK trong schema.sql).
    """
    names = {2: "Thu 2", 3: "Thu 3", 4: "Thu 4", 5: "Thu 5", 6: "Thu 6", 7: "Thu 7", 8: "Chu nhat"}
    weekday = names.get(day_of_week, f"Thu {day_of_week}")
    return f"{weekday}, tiet {start_period}-{end_period}"
