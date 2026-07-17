"""Safety rules applied around the model, not inside the prompt.

Các quy tắc an toàn được áp dụng bên ngoài model, không nằm trong prompt.

A prompt can be talked out of its instructions; code cannot. Anything the university would
consider irreversible (a student ending up in a class they may not take) is therefore gated
here rather than by asking the model nicely in its system instruction.
Prompt có thể bị nói khích để bỏ qua chỉ dẫn, còn code thì không. Vì vậy mọi hành động không
thể hoàn tác (sinh viên bị ghi danh vào một lớp không được phép học) đều bị chặn tại đây,
thay vì chỉ nhờ model tự giữ.

Two things in particular are never taken from the model's own word:

  Whether the student meets the requirements. The model can be told "I already passed
  Discrete Maths, just register me" and it will often believe it. `passed_courses` is read
  from the grade table before the model runs, and that is the only thing the rules consult.

  Whether the student consented. A boolean argument like "already_confirmed" is worth
  nothing: it is the model asserting its own good behaviour. Consent is instead proven by a
  row in pending_registrations created in an *earlier* turn, which means the student saw the
  class read back to them and sent another message. The model cannot write that message.

Hai điều dưới đây tuyệt đối không lấy theo lời model:

  Sinh viên có đủ điều kiện hay không. Sinh viên hoàn toàn có thể nói "em học Toán rời rạc
  rồi mà, đăng ký đi" và model rất dễ tin theo. `passed_courses` được đọc từ bảng điểm trước
  khi model chạy, và đó là thứ duy nhất mà các quy tắc ở đây nhìn vào.

  Sinh viên có đồng ý hay không. Một tham số kiểu "da_xac_nhan" thì không có giá trị gì: đó
  chỉ là model tự khẳng định mình ngoan. Sự đồng ý phải được chứng minh bằng một dòng trong
  pending_registrations được tạo từ một lượt TRƯỚC ĐÓ, nghĩa là sinh viên đã nghe đọc lại
  thông tin lớp và gửi thêm một tin nhắn nữa. Tin nhắn đó model không thể viết thay.

This module is deliberately pure: no database, no clock, no network. Everything it needs is
handed to it in a TurnContext, which is what makes these rules cheap to test.
Module này cố ý giữ thuần khiết: không database, không đồng hồ, không mạng. Mọi thứ nó cần
đều được đưa vào qua TurnContext, nhờ vậy các quy tắc này rất dễ kiểm thử.
"""

from dataclasses import dataclass, field

# Tools that touch no student's private data at all, so they run unconditionally. The
# regulation and the list of open classes are not anyone's secret.
# Các tool không đụng tới dữ liệu riêng của sinh viên nào cả, nên được chạy vô điều kiện. Quy
# chế và danh sách lớp đang mở không phải bí mật của ai.
PUBLIC_TOOLS = frozenset({"tim_kiem_quy_che", "tim_lop_hoc_phan"})

# Tools that read the student's own record. They take no student id: the assistant serves
# exactly one authenticated student, so the tools read it from the TurnContext instead of
# from the model's arguments. The safest way to stop the model from naming someone else's
# student id is to never give it a field to put one in.
# Các tool đọc hồ sơ của chính sinh viên. Chúng không nhận tham số mã sinh viên: trợ lý chỉ
# phục vụ đúng một sinh viên đã xác thực, nên tool đọc mã sinh viên từ TurnContext chứ không
# đọc từ tham số của model. Cách chắc chắn nhất để model không thể nêu ra mã sinh viên của
# người khác là đừng bao giờ cho nó một ô trống để điền vào.
STUDENT_READ_TOOLS = frozenset(
    {"tra_cuu_bang_diem", "tra_cuu_tien_do_hoc_tap", "tinh_gpa_du_kien"}
)

# Preparing a registration enrols nobody; it only writes down what the student asked for.
# Chuẩn bị một lệnh đăng ký không ghi danh ai cả; nó chỉ ghi lại nguyện vọng của sinh viên.
REGISTER_PREPARE_TOOL = "dang_ky_hoc_phan"

# Confirming is the one call that actually puts the student in the class.
# Xác nhận là lệnh gọi duy nhất thực sự đưa sinh viên vào lớp.
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

    Một lớp học phần sinh viên đang hỏi tới, kèm mọi thứ mà các quy tắc cần để phân xử.
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

    Một lớp sinh viên đã đăng ký trong học kỳ này.
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

    Một lệnh đăng ký đã được chuẩn bị trước đó, đang chờ sinh viên xác nhận.
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

    Tất cả những gì guardrail được phép tin, thu thập trước khi model chạy.

    None of this comes from the model: the student's identity comes from the authentication
    layer, the grades and enrolments come from the database, and the turn id is minted by the
    service for this one student message.
    Không thứ nào trong đây đến từ model: danh tính sinh viên đến từ tầng xác thực, bảng điểm
    và các lớp đã đăng ký đến từ database, còn turn id do chính dịch vụ sinh ra cho đúng một
    tin nhắn này của sinh viên.
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
    # Lớp mà lời gọi đang được kiểm tra nhắc tới. Với một lệnh xác nhận, đây là lớp mà dòng
    # pending trỏ tới, không phải lớp do model tự chọn.
    target: ClassSection | None = None
    pending: PendingRegistration | None = None

    @property
    def registered_credits(self) -> int:
        return sum(item.credits for item in self.registered)


@dataclass(frozen=True)
class Decision:
    """The guardrail's verdict on one tool call: allowed or not, and why not.

    Phán quyết của guardrail cho một lệnh gọi tool: cho phép hay không, và vì sao không.
    """

    allowed: bool
    note: str | None = None


def check_tool_call(tool_name: str, arguments: dict, context: TurnContext) -> Decision:
    """Decide whether a tool call the model requested may actually run.

    Quyết định xem lệnh gọi tool mà model yêu cầu có được phép chạy thật hay không.
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

    Kiểm tra lệnh đăng ký mà model muốn ghi lại. Chưa ai được ghi danh cả.

    The checks run here as well as at confirmation time so the student is told straight away
    that they are missing a prerequisite, instead of being asked to confirm a registration
    that was never going to be allowed.
    Các kiểm tra vẫn chạy ở đây chứ không đợi đến lúc xác nhận, để sinh viên được báo ngay là
    mình thiếu môn tiên quyết, thay vì bị hỏi xác nhận một lệnh đăng ký vốn dĩ không bao giờ
    được phép thực hiện.
    """
    if context.target is None:
        return Decision(allowed=False, note="Khong tim thay lop hoc phan nay.")
    return check_registration_rules(context.target, context)


def _check_register_confirm(context: TurnContext) -> Decision:
    """Check the one call that actually enrols the student.

    Kiểm tra lệnh gọi duy nhất thực sự ghi danh sinh viên vào lớp.

    Everything is read from the pending row, never from the model's arguments: the model
    supplies only the slip code, and even that is verified against the row.
    Mọi thứ đều đọc từ dòng pending, không đọc từ tham số của model: model chỉ cung cấp mã
    phiếu, và ngay cả mã đó cũng được đối chiếu lại với dòng pending.
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
    # Một phiếu của người khác, hoặc của một hội thoại khác, thì sinh viên này không có quyền
    # xác nhận.
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
    # Trái tim của quy tắc: một lệnh đăng ký được chuẩn bị ngay trong tin nhắn này của sinh
    # viên thì không thể được xác nhận cũng bởi chính tin nhắn đó. Xác nhận phải trả bằng một
    # tin nhắn riêng của sinh viên, mà model thì không có cách nào gửi thay - nên "sinh viên
    # đã đồng ý" không còn là thứ model muốn khẳng định sao cũng được.
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
    # Các quy tắc được kiểm tra lại, và lần này mới là lần kiểm tra có hiệu lực. Giữa hai lượt,
    # sinh viên có thể đã đăng ký thêm một lớp khác, đủ để đẩy em vượt trần tín chỉ hoặc rơi
    # vào một tình huống trùng lịch chưa hề tồn tại lúc phiếu được ghi ra.
    return check_registration_rules(context.target, context)


def check_registration_rules(target: ClassSection, context: TurnContext) -> Decision:
    """The six rules that decide whether a student may join a class.

    Sáu quy tắc quyết định sinh viên có được vào một lớp hay không.
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
    # Việc kiểm tra tiên quyết đọc từ bảng điểm, không đọc từ cuộc hội thoại. Một sinh viên
    # khẳng định mình đã đạt môn tiên quyết, và một model tin theo lời đó, không làm thay đổi
    # điều gì ở đây.
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
        # Cả hai lịch học đều được nói rõ. Nếu chỉ nêu một lịch, câu văn sẽ đọc ra thành lịch
        # của lớp kia, và model sẽ lặp lại đúng sự nhầm lẫn đó cho sinh viên nghe.
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
    """The class already registered under this course code, if any.

    Lớp đã đăng ký thuộc đúng mã học phần này, nếu có.
    """
    for item in context.registered:
        if item.course_code == course_code:
            return item
    return None


def _find_clash(target: ClassSection, context: TurnContext) -> RegisteredClass | None:
    """The first already-registered class whose timetable overlaps the target's.

    Lớp đầu tiên trong số các lớp đã đăng ký có lịch học giao với lịch của lớp đang xét.
    """
    for item in context.registered:
        if item.day_of_week != target.day_of_week:
            continue
        # Two period ranges on the same weekday overlap unless one ends before the other
        # begins. Sharing a single period is already a clash.
        # Hai khoảng tiết trong cùng một thứ là giao nhau trừ khi khoảng này kết thúc trước
        # khi khoảng kia bắt đầu. Chỉ cần trùng đúng một tiết đã là trùng lịch.
        if target.start_period <= item.end_period and item.start_period <= target.end_period:
            return item
    return None


def mask_student_id(student_id: str) -> str:
    """Keep only the last four characters of a student id.

    Chỉ giữ lại bốn ký tự cuối của mã sinh viên.
    """
    value = student_id.strip()
    if len(value) <= 4:
        return value
    return "*" * (len(value) - 4) + value[-4:]
