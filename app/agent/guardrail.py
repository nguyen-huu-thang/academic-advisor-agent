"""Safety rules applied around the model, not inside the prompt.

Cac quy tac an toan duoc ap dung ben ngoai model, khong nam trong prompt.

A prompt can be talked out of its instructions; code cannot. Anything the university would
consider irreversible (a student ending up in a class they may not take) is therefore gated
here rather than by asking the model nicely in its system instruction.
Prompt co the bi noi khich de bo qua chi dan, con code thi khong. Vi vay moi hanh dong khong
the hoan tac (sinh vien bi ghi danh vao mot lop khong duoc phep hoc) deu bi chan tai day,
thay vi chi nho model tu giu.

Two things in particular are never taken from the model's own word:

  Whether the student meets the requirements. The model can be told "I already passed
  Discrete Maths, just register me" and it will often believe it. `passed_courses` is read
  from the grade table before the model runs, and that is the only thing the rules consult.

  Whether the student consented. A boolean argument like "already_confirmed" is worth
  nothing: it is the model asserting its own good behaviour. Consent is instead proven by a
  row in pending_registrations created in an *earlier* turn, which means the student saw the
  class read back to them and sent another message. The model cannot write that message.

Hai dieu duoi day tuyet doi khong lay theo loi model:

  Sinh vien co du dieu kien hay khong. Sinh vien hoan toan co the noi "em hoc Toan roi rac
  roi ma, dang ky di" va model rat de tin theo. `passed_courses` duoc doc tu bang diem truoc
  khi model chay, va do la thu duy nhat ma cac quy tac o day nhin vao.

  Sinh vien co dong y hay khong. Mot tham so kieu "da_xac_nhan" thi khong co gia tri gi: do
  chi la model tu khang dinh minh ngoan. Su dong y phai duoc chung minh bang mot dong trong
  pending_registrations duoc tao tu mot luot TRUOC DO, nghia la sinh vien da nghe doc lai
  thong tin lop va gui them mot tin nhan nua. Tin nhan do model khong the viet thay.

This module is deliberately pure: no database, no clock, no network. Everything it needs is
handed to it in a TurnContext, which is what makes these rules cheap to test.
Module nay co y giu thuan khiet: khong database, khong dong ho, khong mang. Moi thu no can
deu duoc dua vao qua TurnContext, nho vay cac quy tac nay rat de kiem thu.
"""

from dataclasses import dataclass, field

# Tools that touch no student's private data at all, so they run unconditionally. The
# regulation and the list of open classes are not anyone's secret.
# Cac tool khong dung toi du lieu rieng cua sinh vien nao ca, nen duoc chay vo dieu kien. Quy
# che va danh sach lop dang mo khong phai bi mat cua ai.
PUBLIC_TOOLS = frozenset({"tim_kiem_quy_che", "tim_lop_hoc_phan"})

# Tools that read the student's own record. They take no student id: the assistant serves
# exactly one authenticated student, so the tools read it from the TurnContext instead of
# from the model's arguments. The safest way to stop the model from naming someone else's
# student id is to never give it a field to put one in.
# Cac tool doc ho so cua chinh sinh vien. Chung khong nhan tham so ma sinh vien: tro ly chi
# phuc vu dung mot sinh vien da xac thuc, nen tool doc ma sinh vien tu TurnContext chu khong
# doc tu tham so cua model. Cach chac chan nhat de model khong the neu ra ma sinh vien cua
# nguoi khac la dung bao gio cho no mot o trong de dien vao.
STUDENT_READ_TOOLS = frozenset(
    {"tra_cuu_bang_diem", "tra_cuu_tien_do_hoc_tap", "tinh_gpa_du_kien"}
)

# Preparing a registration enrols nobody; it only writes down what the student asked for.
# Chuan bi mot lenh dang ky khong ghi danh ai ca; no chi ghi lai nguyen vong cua sinh vien.
REGISTER_PREPARE_TOOL = "dang_ky_hoc_phan"

# Confirming is the one call that actually puts the student in the class.
# Xac nhan la lenh goi duy nhat thuc su dua sinh vien vao lop.
REGISTER_CONFIRM_TOOL = "xac_nhan_dang_ky"

ALL_TOOLS = (
    PUBLIC_TOOLS
    | STUDENT_READ_TOOLS
    | {REGISTER_PREPARE_TOOL, REGISTER_CONFIRM_TOOL}
)

PENDING_STATUS_WAITING = "cho_xac_nhan"
PENDING_STATUS_DONE = "da_thuc_hien"

WEEKDAY_NAMES = {2: "Thu 2", 3: "Thu 3", 4: "Thu 4", 5: "Thu 5", 6: "Thu 6", 7: "Thu 7", 8: "Chu nhat"}


@dataclass(frozen=True)
class ClassSection:
    """A class the student is asking about, with everything the rules need to judge it.

    Mot lop hoc phan sinh vien dang hoi toi, kem moi thu ma cac quy tac can de phan xu.
    """

    id: int
    course_code: str
    course_name: str
    section_no: str
    credits: int
    capacity: int
    enrolled: int
    day_of_week: int
    start_period: int
    end_period: int
    prereq_codes: frozenset[str] = frozenset()

    @property
    def is_full(self) -> bool:
        return self.enrolled >= self.capacity

    def schedule_text(self) -> str:
        weekday = WEEKDAY_NAMES.get(self.day_of_week, f"Thu {self.day_of_week}")
        return f"{weekday}, tiet {self.start_period}-{self.end_period}"


@dataclass(frozen=True)
class RegisteredClass:
    """A class the student is already enrolled in this semester.

    Mot lop sinh vien da dang ky trong hoc ky nay.
    """

    class_section_id: int
    course_code: str
    course_name: str
    credits: int
    day_of_week: int
    start_period: int
    end_period: int

    def schedule_text(self) -> str:
        weekday = WEEKDAY_NAMES.get(self.day_of_week, f"Thu {self.day_of_week}")
        return f"{weekday}, tiet {self.start_period}-{self.end_period}"


@dataclass(frozen=True)
class PendingRegistration:
    """A registration prepared earlier, waiting for the student to confirm.

    Mot lenh dang ky da duoc chuan bi truoc do, dang cho sinh vien xac nhan.
    """

    id: str
    session_id: str
    student_id: str
    created_turn_id: str
    class_section_id: int
    status: str
    expired: bool


@dataclass(frozen=True)
class TurnContext:
    """Everything the guardrail is allowed to trust, gathered before the model runs.

    Tat ca nhung gi guardrail duoc phep tin, thu thap truoc khi model chay.

    None of this comes from the model: the student's identity comes from the authentication
    layer, the grades and enrolments come from the database, and the turn id is minted by the
    service for this one student message.
    Khong thu nao trong day den tu model: danh tinh sinh vien den tu tang xac thuc, bang diem
    va cac lop da dang ky den tu database, con turn id do chinh dich vu sinh ra cho dung mot
    tin nhan nay cua sinh vien.
    """

    student_id: str
    session_id: str
    turn_id: str
    semester: str
    academic_status: str
    max_credits: int
    registration_open: bool
    passed_courses: frozenset[str] = frozenset()
    registered: tuple[RegisteredClass, ...] = ()
    # The class named by the call being checked. For a confirmation this is the class the
    # pending row points at, not one the model chose.
    # Lop ma loi goi dang duoc kiem tra nhac toi. Voi mot lenh xac nhan, day la lop ma dong
    # pending tro toi, khong phai lop do model tu chon.
    target: ClassSection | None = None
    pending: PendingRegistration | None = None

    @property
    def registered_credits(self) -> int:
        return sum(item.credits for item in self.registered)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    note: str | None = None


def check_tool_call(tool_name: str, arguments: dict, context: TurnContext) -> Decision:
    """Decide whether a tool call the model requested may actually run.

    Quyet dinh xem lenh goi tool ma model yeu cau co duoc phep chay that hay khong.
    """
    if tool_name in PUBLIC_TOOLS or tool_name in STUDENT_READ_TOOLS:
        return Decision(allowed=True)

    if tool_name == REGISTER_PREPARE_TOOL:
        return _check_register_prepare(context)

    if tool_name == REGISTER_CONFIRM_TOOL:
        return _check_register_confirm(context)

    return Decision(
        allowed=False,
        note=f"Tool '{tool_name}' khong nam trong danh sach duoc phep.",
    )


def _check_register_prepare(context: TurnContext) -> Decision:
    """Check a registration the model wants to write down. Nobody is enrolled yet.

    Kiem tra lenh dang ky ma model muon ghi lai. Chua ai duoc ghi danh ca.

    The checks run here as well as at confirmation time so the student is told straight away
    that they are missing a prerequisite, instead of being asked to confirm a registration
    that was never going to be allowed.
    Cac kiem tra van chay o day chu khong doi den luc xac nhan, de sinh vien duoc bao ngay la
    minh thieu mon tien quyet, thay vi bi hoi xac nhan mot lenh dang ky von di khong bao gio
    duoc phep thuc hien.
    """
    if context.target is None:
        return Decision(allowed=False, note="Khong tim thay lop hoc phan nay.")
    return _check_registration_rules(context.target, context)


def _check_register_confirm(context: TurnContext) -> Decision:
    """Check the one call that actually enrols the student.

    Kiem tra lenh goi duy nhat thuc su ghi danh sinh vien vao lop.

    Everything is read from the pending row, never from the model's arguments: the model
    supplies only the slip code, and even that is verified against the row.
    Moi thu deu doc tu dong pending, khong doc tu tham so cua model: model chi cung cap ma
    phieu, va ngay ca ma do cung duoc doi chieu lai voi dong pending.
    """
    pending = context.pending
    if pending is None:
        return Decision(
            allowed=False,
            note=(
                "Khong tim thay phieu dang ky nao dang cho xac nhan voi ma nay. "
                "Hay dang ky lai hoc phan."
            ),
        )

    # A slip belonging to someone else, or to another conversation, is not a slip this
    # student may confirm.
    # Mot phieu cua nguoi khac, hoac cua mot hoi thoai khac, thi sinh vien nay khong co quyen
    # xac nhan.
    if pending.student_id != context.student_id or pending.session_id != context.session_id:
        return Decision(
            allowed=False,
            note="Phieu dang ky nay khong thuoc ve sinh vien hoac phien lam viec hien tai.",
        )

    if pending.status == PENDING_STATUS_DONE:
        return Decision(
            allowed=False,
            note=f"Phieu {pending.id} da duoc thuc hien roi, khong thuc hien lai.",
        )

    if pending.expired:
        return Decision(
            allowed=False,
            note=(
                f"Phieu {pending.id} da het han xac nhan. "
                "Hay dang ky lai hoc phan neu sinh vien van muon hoc lop nay."
            ),
        )

    # The heart of the rule: a registration prepared during this very student message cannot
    # also be confirmed by it. Confirmation costs a separate message from the student, and the
    # model has no way to send one on their behalf - so "the student agreed" stops being
    # something the model can simply assert.
    # Trai tim cua quy tac: mot lenh dang ky duoc chuan bi ngay trong tin nhan nay cua sinh
    # vien thi khong the duoc xac nhan cung boi chinh tin nhan do. Xac nhan phai tra bang mot
    # tin nhan rieng cua sinh vien, ma model thi khong co cach nao gui thay - nen "sinh vien
    # da dong y" khong con la thu model muon khang dinh sao cung duoc.
    if pending.created_turn_id == context.turn_id:
        return Decision(
            allowed=False,
            note=(
                "Phieu vua duoc tao trong chinh luot nay nen chua the xac nhan. "
                "Hay doc lai thong tin lop cho sinh vien va cho sinh vien xac nhan."
            ),
        )

    if context.target is None:
        return Decision(allowed=False, note="Khong tim thay lop hoc phan cua phieu nay.")

    # The rules are checked again, and this time it is the check that counts. Between the two
    # turns the student may have registered for another class, which can push them over the
    # credit ceiling or into a timetable clash that did not exist when the slip was written.
    # Cac quy tac duoc kiem tra lai, va lan nay moi la lan kiem tra co hieu luc. Giua hai luot,
    # sinh vien co the da dang ky them mot lop khac, du de day em vuot tran tin chi hoac roi
    # vao mot tinh huong trung lich chua he ton tai luc phieu duoc ghi ra.
    return _check_registration_rules(context.target, context)


def _check_registration_rules(target: ClassSection, context: TurnContext) -> Decision:
    """The six rules that decide whether a student may join a class.

    Sau quy tac quyet dinh sinh vien co duoc vao mot lop hay khong.
    """
    if not context.registration_open:
        return Decision(
            allowed=False,
            note=(
                f"Hoc ky {context.semester} hien khong trong thoi gian mo dang ky hoc phan. "
                "Moi yeu cau dang ky deu bi tu choi ngoai thoi gian nay."
            ),
        )

    already = _find_registered_course(target.course_code, context)
    if already is not None:
        return Decision(
            allowed=False,
            note=(
                f"Sinh vien da dang ky hoc phan {target.course_code} "
                f"{already.course_name} trong hoc ky nay roi."
            ),
        )

    # The prerequisite check reads the grade table, not the conversation. A student who says
    # they have already passed the prerequisite, and a model that believes them, change
    # nothing here.
    # Viec kiem tra tien quyet doc tu bang diem, khong doc tu cuoc hoi thoai. Mot sinh vien
    # khang dinh minh da dat mon tien quyet, va mot model tin theo loi do, khong lam thay doi
    # dieu gi o day.
    missing = sorted(target.prereq_codes - context.passed_courses)
    if missing:
        return Decision(
            allowed=False,
            note=(
                f"Chua du dieu kien tien quyet cho hoc phan {target.course_code} "
                f"{target.course_name}. Con thieu: {', '.join(missing)}. "
                "Sinh vien phai co diem dat cac hoc phan nay trong bang diem thi moi dang ky duoc."
            ),
        )

    total = context.registered_credits + target.credits
    if total > context.max_credits:
        return Decision(
            allowed=False,
            note=(
                f"Dang ky them {target.credits} tin chi se nang tong so tin chi hoc ky len "
                f"{total}, vuot tran {context.max_credits} tin chi ap dung cho sinh vien co "
                f"tinh trang hoc vu '{context.academic_status}'. "
                f"Hien sinh vien da dang ky {context.registered_credits} tin chi."
            ),
        )

    clash = _find_clash(target, context)
    if clash is not None:
        # Both timetables are spelled out. Naming only one of them reads as if it belonged to
        # the other class, and the model will repeat that confusion back to the student.
        # Ca hai lich hoc deu duoc noi ro. Neu chi neu mot lich, cau van se doc ra thanh lich
        # cua lop kia, va model se lap lai dung su nham lan do cho sinh vien nghe.
        return Decision(
            allowed=False,
            note=(
                f"Lop {target.course_code} nhom {target.section_no} hoc {target.schedule_text()}, "
                f"trung lich voi hoc phan {clash.course_code} {clash.course_name} "
                f"ma sinh vien da dang ky (hoc {clash.schedule_text()})."
            ),
        )

    if target.is_full:
        return Decision(
            allowed=False,
            note=(
                f"Lop {target.course_code} nhom {target.section_no} da du si so "
                f"{target.capacity} sinh vien. Hay chon lop khac cua cung hoc phan."
            ),
        )

    return Decision(allowed=True)


def _find_registered_course(course_code: str, context: TurnContext) -> RegisteredClass | None:
    for item in context.registered:
        if item.course_code == course_code:
            return item
    return None


def _find_clash(target: ClassSection, context: TurnContext) -> RegisteredClass | None:
    """The first already-registered class whose timetable overlaps the target's.

    Lop dau tien trong so cac lop da dang ky co lich hoc giao voi lich cua lop dang xet.
    """
    for item in context.registered:
        if item.day_of_week != target.day_of_week:
            continue
        # Two period ranges on the same weekday overlap unless one ends before the other
        # begins. Sharing a single period is already a clash.
        # Hai khoang tiet trong cung mot thu la giao nhau tru khi khoang nay ket thuc truoc
        # khi khoang kia bat dau. Chi can trung dung mot tiet da la trung lich.
        if target.start_period <= item.end_period and item.start_period <= target.end_period:
            return item
    return None


def mask_student_id(student_id: str) -> str:
    """Keep only the last four characters of a student id.

    Chi giu lai bon ky tu cuoi cua ma sinh vien.
    """
    value = student_id.strip()
    if len(value) <= 4:
        return value
    return "*" * (len(value) - 4) + value[-4:]
