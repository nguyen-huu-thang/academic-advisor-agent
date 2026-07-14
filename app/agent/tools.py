"""The tools the agent may call, and their implementations.

Cac tool ma agent duoc phep goi, kem phan cai dat.

Each tool is declared to Gemini as a function schema; the model reads the descriptions to
decide which one answers the student's question. The implementations read from PostgreSQL, so
every number the assistant quotes comes from real data rather than the model's own memory.
Moi tool duoc khai bao voi Gemini duoi dang function schema; model doc phan mo ta de tu quyet
dinh goi tool nao. Phan cai dat doc du lieu tu PostgreSQL, nen moi con so ma tro ly dua ra deu
den tu du lieu that chu khong phai model tu nho.

Registering is split across two tools on purpose. `dang_ky_hoc_phan` writes the request down
and hands back a slip code; `xac_nhan_dang_ky` is the only one that puts the student in the
class. The split is what turns "the student agreed" into something the service can check
rather than something the model can claim - see guardrail.py.
Viec dang ky duoc tach lam hai tool la co chu dich. `dang_ky_hoc_phan` chi ghi lai nguyen vong
va tra ve mot ma phieu; `xac_nhan_dang_ky` moi la tool duy nhat dua sinh vien vao lop. Chinh
viec tach doi nay bien "sinh vien da dong y" thanh thu ma dich vu kiem tra duoc, thay vi thu ma
model muon noi sao cung duoc - xem guardrail.py.

The tools that read a student's record take no student id. The assistant serves one
authenticated student per session, so the id comes from the TurnContext. A field the model
cannot fill in is a field it cannot abuse.
Cac tool doc ho so sinh vien khong nhan tham so ma sinh vien. Tro ly chi phuc vu dung mot sinh
vien da xac thuc trong moi phien, nen ma sinh vien lay tu TurnContext. Mot o trong ma model
khong dien duoc thi cung la mot o trong no khong lam dung sai duoc.
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
# Sinh vien co bao lau de xac nhan truoc khi phieu da chuan bi bi coi la cu. Du dai de doc thong
# tin lop va tra loi, du ngan de mot phieu bi bo quen khong the bi xac nhan nham sau nhieu gio.
CONFIRM_TTL_MINUTES = 10


class RegistrationRejected(Exception):
    """A registration that cannot go through. Raised inside the transaction so it rolls back.

    Mot lenh dang ky khong the thuc hien. Duoc nem ra ben trong transaction de transaction tu
    dong bi huy bo.
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
# Nap du lieu cho luot hien tai
#
# Everything the guardrail is allowed to trust is read here, before the model runs. Reading it
# up front is what lets guardrail.py stay a pure function of its inputs.
# Moi thu guardrail duoc phep tin deu duoc doc o day, truoc khi model chay. Chinh viec doc san
# tu dau la thu cho phep guardrail.py van la mot ham thuan tuy cua dau vao.


def load_student(student_id: str) -> dict | None:
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
# Moi ham duoi day co hai dang, va do la co y. Dang cong khai tu mo ket noi rieng, va la dang ma
# agent goi truoc khi model chay. Dang rieng tu thi nhan mot ket noi duoc dua cho, va la dang ma
# transaction xac nhan goi - boi mot su that doc lai tren mot ket noi KHAC thi se duoc doc ben
# ngoai transaction, se khong nhin thay cac khoa ma transaction do dang giu, va vi vay se dung la
# cai su that cu ky ma cai khoa sinh ra de tranh.


def load_passed_courses(student_id: str) -> frozenset[str]:
    """The courses this student has actually passed, straight from the grade table.

    Cac hoc phan sinh vien nay thuc su da dat, lay thang tu bang diem.

    This is the only answer to "have I done the prerequisite" that the service accepts. What
    the student says, and what the model believes them, do not enter into it.
    Day la cau tra loi duy nhat cho cau hoi "em da hoc mon tien quyet chua" ma dich vu chap
    nhan. Sinh vien noi gi, va model co tin theo hay khong, khong lien quan gi o day.
    """
    with get_connection() as conn:
        return _passed_courses(conn, student_id)


def _passed_courses(conn, student_id: str) -> frozenset[str]:
    rows = conn.execute(
        "SELECT course_code FROM grades WHERE student_id = %s AND passed",
        (student_id,),
    ).fetchall()
    return frozenset(row["course_code"] for row in rows)


def load_registered(student_id: str, semester: str) -> tuple[RegisteredClass, ...]:
    with get_connection() as conn:
        return _registered(conn, student_id, semester)


def _registered(conn, student_id: str, semester: str) -> tuple[RegisteredClass, ...]:
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

    Dang ky hoc phan co dang mo hay khong, do dong ho cua database quyet dinh.

    The comparison is made by PostgreSQL rather than by the application so a clock skew
    between the two cannot reopen a window that has already closed.
    Phep so sanh do PostgreSQL thuc hien chu khong phai ung dung, de lech dong ho giua hai ben
    khong the mo lai mot dot dang ky von da dong.
    """
    with get_connection() as conn:
        return _registration_open(conn, semester)


def _registration_open(conn, semester: str) -> bool:
    row = conn.execute(
        """
        SELECT (now() BETWEEN opens_at AND closes_at) AS is_open
        FROM registration_windows WHERE semester = %s
        """,
        (semester,),
    ).fetchone()
    return bool(row and row["is_open"])


def load_class_section(section_id: int, semester: str) -> ClassSection | None:
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

    Doc mot phieu dang ky da chuan bi, de PostgreSQL tu quyet dinh no het han hay chua.
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
        course_code = str(arguments.get("ma_mon") or "").strip().upper()

        with get_connection() as conn:
            if course_code:
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

            prereq_rows = conn.execute(
                """
                SELECT course_code, prereq_code FROM prerequisites
                WHERE course_code = ANY(%s)
                """,
                ([row["course_code"] for row in rows],),
            ).fetchall()

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
        student = load_student(context.student_id)
        if student is None:
            return {"loi": "Khong tim thay sinh vien."}

        with get_connection() as conn:
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

        Tinh lai GPA nhu the sinh vien dat cac muc diem gia dinh o cac hoc phan da neu.

        A repeated course replaces the earlier attempt rather than being added alongside it,
        which is what the regulation says about retaking a course. Adding it twice would let a
        student "improve" their GPA by simply retaking a course they had already passed well.
        Mot hoc phan hoc lai se thay the lan hoc truoc chu khong duoc cong them ben canh, dung
        nhu quy che quy dinh ve hoc lai. Neu cong ca hai lan, sinh vien co the "cai thien" GPA
        chi bang cach hoc lai mot mon von da dat diem cao.
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

        Buoc mot: ghi lai nguyen vong dang ky va tra ve mot ma phieu. Chua ai duoc ghi danh o
        buoc nay.
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
            # Ma phieu co y kho doan: no la ma ma tin nhan tiep theo cua sinh vien se doi chieu
            # vao, nen khong duoc phep doan mo ra phieu cho cua nguoi khac.
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

        Buoc hai: thuc hien lenh dang ky sinh vien da xac nhan. Lop day len o day.

        The guardrail has already approved this call, but nothing here trusts that: the slip is
        claimed again under lock and the seat is counted again under lock. The guardrail read
        the seat count a moment ago, outside any transaction, and by now another student may
        have taken the last seat.
        Guardrail da duyet loi goi nay, nhung o day khong tin vao dieu do: phieu duoc gianh lai
        mot lan nua trong trang thai khoa, va cho ngoi cung duoc dem lai trong trang thai khoa.
        Guardrail doc si so tu mot luc truoc, ben ngoai moi transaction, va den gio nay mot sinh
        vien khac hoan toan co the da lay mat cho cuoi cung.
        """
        slip_id = str(arguments.get("ma_phieu", "")).strip()

        try:
            with get_connection() as conn:
                with conn.transaction():
                    return execute_registration(conn, slip_id, self._settings)
        except RegistrationRejected as rejection:
            # Raised inside the transaction, so PostgreSQL has rolled everything back, including
            # the slip we had marked as executed. It goes back to waiting.
            # Duoc nem ra ben trong transaction, nen PostgreSQL da huy bo toan bo, ke ca dong
            # phieu vua bi danh dau la da thuc hien. No quay lai trang thai cho.
            return {"loi": str(rejection)}


def execute_registration(conn, slip_id: str, settings: Settings) -> dict:
    """Enrol the student named on the slip, or refuse. Runs inside a transaction.

    Ghi danh sinh vien ghi tren phieu, hoac tu choi. Chay ben trong mot transaction.

    The guardrail has already approved this call, and none of that approval is trusted here. It
    was made against facts read at the start of the turn, on another connection, outside every
    transaction. Between then and now another confirmation may have gone through - for this same
    student, or for this same class - and every one of those facts may have moved.

    So the six rules are run again, here, over facts re-read under lock. This is the run that
    counts; the earlier one only existed to tell the student early.

    Guardrail da duyet loi goi nay, va o day khong tin vao su phe duyet do chut nao. No duoc dua
    ra dua tren nhung su that doc tu dau luot, tren mot ket noi khac, ben ngoai moi transaction.
    Tu luc do den gio, mot lenh xac nhan khac hoan toan co the da di qua - cua chinh sinh vien
    nay, hoac vao chinh lop nay - va moi su that kia deu co the da doi.

    Vi vay sau quy tac duoc chay lai, tai day, tren nhung su that doc lai duoi khoa. Day moi la
    lan chay co hieu luc; lan truoc do chi ton tai de bao som cho sinh vien.

    Exposed at module level rather than kept as a method so the concurrency test can fire it
    from many threads at once without standing up an agent.
    Ham nay dat o cap module thay vi la mot method, de bai test tranh chap dong thoi co the goi
    thang no tu nhieu luong cung luc ma khong can dung len ca mot agent.
    """
    # Claim the slip by flipping its status, and only proceed if this statement is the one that
    # flipped it. A second confirmation of the same code - whether from a retried request or
    # from the model calling the tool twice - updates no rows and enrols nobody.
    # Gianh lay phieu bang cach lat trang thai cua no, va chi di tiep neu chinh cau lenh nay la
    # cau da lat duoc. Mot lan xac nhan thu hai tren cung ma phieu, du den tu request bi gui lai
    # hay tu viec model goi tool hai lan, se khong cap nhat dong nao va khong ghi danh ai ca.
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
    # Khoa dong SINH VIEN, truoc khi doc bat cu thu gi khac ve sinh vien nay.
    #
    # Day la cai khoa bit lai lo hong ma khoa lop khong bao gio cham toi. Khoa lop bao ve si so,
    # von bi tranh chap giua NHUNG sinh vien khac nhau. Nhung tran tin chi va viec trung lich lai
    # la thuoc tinh cua tap cac lop cua MOT sinh vien, va chung bi tranh chap khi chinh sinh vien
    # do xac nhan hai phieu cung luc - hai tab trinh duyet, mot request bi gui lai, hay mot model
    # goi ca hai lenh xac nhan trong cung mot cau tra loi. Ca hai deu se doc cung mot danh sach lop
    # cu ky, ca hai deu thay con cho duoi tran, va ca hai deu di qua, de lai sinh vien trong hai
    # lop trung lich nhau hoac vuot tran tin chi.
    #
    # Giu dong nay khien moi lenh xac nhan cua sinh vien nay phai xep hang sau lenh dang chay, nen
    # danh sach lop doc o duoi la danh sach tai thoi diem nay, khong phai tu dau luot.
    #
    # The order matters and is fixed everywhere: student first, then class. Two transactions that
    # took them in opposite orders would sooner or later wait on each other for ever.
    # Thu tu khoa la quan trong va duoc co dinh o khap noi: sinh vien truoc, lop sau. Hai
    # transaction lay hai khoa nay theo hai thu tu nguoc nhau thi som muon cung se cho nhau mai mai.
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
    # Khoa dong cua lop. Moi lenh xac nhan khac vao cung lop nay tu gio phai xep hang sau cau
    # lenh nay, nen si so doc o duoi khong the doi ngay duoi chan ta trong khoang giua luc doc
    # va luc hanh dong.
    #
    # Cai khoa nay khong phai la thu giu cho du lieu dung - rang buoc CHECK tren class_sections
    # da lam viec do roi, va dieu nay da duoc do that: khi bo khoa di, 19 tren 20 lenh xac nhan
    # dong thoi van that bai, nhung chung that bai duoi dang CheckViolation do PostgreSQL nem
    # ra. Thu ma cai khoa mua ve la *chat luong cua loi tu choi*. Khong co no, 19 sinh vien nhan
    # mot loi rang buoc tho, lo ra ngoai thanh "tool bi loi"; co no, ho nhan duoc cau "lop da du
    # si so, em chon lop khac nhe". Du lieu dung va cau tra loi dung la hai bai toan khac nhau,
    # va chung duoc giai bang hai lop phong thu khac nhau.
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
    # Dung lai hoan toan tu nhung gi transaction nay nhin thay, khi dang giu ca hai khoa. Chinh
    # dung ham thuan ma guardrail da dung se duoc chay lai tren no - cac quy tac khong duoc viet
    # lai o day, boi hai ban sao cua sau quy tac roi se co luc lech nhau, va ban sao lech mot cach
    # am tham se dung la ban dang canh lenh ghi.
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
    names = {2: "Thu 2", 3: "Thu 3", 4: "Thu 4", 5: "Thu 5", 6: "Thu 6", 7: "Thu 7", 8: "Chu nhat"}
    weekday = names.get(day_of_week, f"Thu {day_of_week}")
    return f"{weekday}, tiet {start_period}-{end_period}"
