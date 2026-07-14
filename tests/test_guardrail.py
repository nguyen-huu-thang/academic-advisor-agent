"""Tests for the rules that decide what the agent is allowed to do.

Kiem thu cac quy tac quyet dinh agent duoc phep lam gi.

These are the tests that matter most: anything else in the service can be wrong and fixed
later, but a student who ends up enrolled in a class they may not take has to be taken back
out by a human.
Day la nhung bai test quan trong nhat: moi thu khac trong dich vu co sai thi sua sau cung
duoc, nhung mot sinh vien bi ghi danh vao lop khong duoc phep hoc thi phai co nguoi that vao
go ra.
"""

from app.agent.guardrail import (
    ClassSection,
    PendingRegistration,
    RegisteredClass,
    TurnContext,
    check_tool_call,
    mask_student_id,
)

TURN_NOW = "turn-2"
TURN_EARLIER = "turn-1"


def make_section(
    *,
    section_id: int = 1,
    course_code: str = "INT3401",
    credits: int = 3,
    capacity: int = 60,
    enrolled: int = 45,
    day_of_week: int = 3,
    start_period: int = 1,
    end_period: int = 3,
    prereq_codes: frozenset[str] = frozenset({"MAT1101", "INT2010"}),
) -> ClassSection:
    return ClassSection(
        id=section_id,
        course_code=course_code,
        course_name="Tri tue nhan tao",
        section_no="01",
        credits=credits,
        capacity=capacity,
        enrolled=enrolled,
        day_of_week=day_of_week,
        start_period=start_period,
        end_period=end_period,
        prereq_codes=prereq_codes,
    )


def make_registered(
    *,
    course_code: str = "INT2207",
    credits: int = 3,
    day_of_week: int = 4,
    start_period: int = 4,
    end_period: int = 6,
) -> RegisteredClass:
    return RegisteredClass(
        class_section_id=99,
        course_code=course_code,
        course_name="Co so du lieu",
        credits=credits,
        day_of_week=day_of_week,
        start_period=start_period,
        end_period=end_period,
    )


def make_pending(
    *,
    created_turn_id: str = TURN_EARLIER,
    student_id: str = "22021003",
    session_id: str = "s1",
    status: str = "cho_xac_nhan",
    expired: bool = False,
) -> PendingRegistration:
    return PendingRegistration(
        id="DK1A2B",
        session_id=session_id,
        student_id=student_id,
        created_turn_id=created_turn_id,
        class_section_id=1,
        status=status,
        expired=expired,
    )


def make_context(
    *,
    passed_courses: frozenset[str] = frozenset({"MAT1101", "INT2010"}),
    registered: tuple[RegisteredClass, ...] = (),
    max_credits: int = 24,
    academic_status: str = "binh_thuong",
    registration_open: bool = True,
    target: ClassSection | None = None,
    pending: PendingRegistration | None = None,
) -> TurnContext:
    return TurnContext(
        student_id="22021003",
        session_id="s1",
        turn_id=TURN_NOW,
        semester="2026.1",
        academic_status=academic_status,
        max_credits=max_credits,
        registration_open=registration_open,
        passed_courses=passed_courses,
        registered=registered,
        target=target if target is not None else make_section(),
        pending=pending,
    )


# Reading tools
# Cac tool chi doc


def test_public_tool_runs_unconditionally():
    decision = check_tool_call(
        "tim_kiem_quy_che", {"cau_hoi": "dieu kien tot nghiep"}, make_context()
    )
    assert decision.allowed


def test_reading_own_record_needs_no_permission():
    # These tools take no student id, so there is no field for the model to put someone
    # else's id into. They read the student from the TurnContext.
    # Cac tool nay khong nhan tham so ma sinh vien, nen model khong co o trong nao de dien ma
    # cua nguoi khac vao. Chung doc sinh vien tu TurnContext.
    assert check_tool_call("tra_cuu_bang_diem", {}, make_context()).allowed
    assert check_tool_call("tra_cuu_tien_do_hoc_tap", {}, make_context()).allowed


def test_unknown_tool_is_refused():
    decision = check_tool_call("xoa_diem_mon_hoc", {"mon": "MAT1101"}, make_context())
    assert not decision.allowed
    assert "khong nam trong danh sach duoc phep" in decision.note


# Preparing a registration
# Chuan bi mot lenh dang ky


def test_registration_allowed_when_every_rule_is_satisfied():
    assert check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, make_context()).allowed


def test_unknown_class_is_refused():
    context = TurnContext(
        student_id="22021003",
        session_id="s1",
        turn_id=TURN_NOW,
        semester="2026.1",
        academic_status="binh_thuong",
        max_credits=24,
        registration_open=True,
        target=None,
    )
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 999}, context)
    assert not decision.allowed
    assert "Khong tim thay lop" in decision.note


def test_registration_outside_the_window_is_refused():
    decision = check_tool_call(
        "dang_ky_hoc_phan", {"ma_lop": 1}, make_context(registration_open=False)
    )
    assert not decision.allowed
    assert "khong trong thoi gian mo dang ky" in decision.note


def test_registering_the_same_course_twice_is_refused():
    context = make_context(registered=(make_registered(course_code="INT3401"),))
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context)
    assert not decision.allowed
    assert "da dang ky hoc phan INT3401" in decision.note


def test_missing_prerequisite_is_refused_and_named():
    # The student has passed data structures but failed discrete maths, so exactly one
    # prerequisite is missing and the refusal must say which.
    # Sinh vien da dat Cau truc du lieu nhung truot Toan roi rac, nen thieu dung mot mon tien
    # quyet, va loi tu choi phai noi ro thieu mon nao.
    context = make_context(passed_courses=frozenset({"INT2010"}))
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context)
    assert not decision.allowed
    missing_part = decision.note.split("Con thieu:")[1]
    assert "MAT1101" in missing_part
    assert "INT2010" not in missing_part


def test_prerequisite_check_ignores_what_the_model_claims():
    # The model can be talked into asserting the student is eligible. It makes no difference:
    # these arguments are never read, only the grade table is.
    # Model hoan toan co the bi noi khich de tu khang dinh sinh vien du dieu kien. Dieu do
    # khong thay doi gi: cac tham so nay khong bao gio duoc doc, chi bang diem duoc doc.
    context = make_context(passed_courses=frozenset())
    decision = check_tool_call(
        "dang_ky_hoc_phan",
        {"ma_lop": 1, "da_du_dieu_kien": True, "sinh_vien_xac_nhan": True},
        context,
    )
    assert not decision.allowed
    assert "MAT1101" in decision.note


def test_credit_ceiling_is_enforced():
    # A student on academic warning has a ceiling of 18. She sits at 17 credits, so one more
    # 3-credit course would take her to 20.
    # Sinh vien bi canh bao hoc vu co tran 18 tin. Em dang o 17 tin chi, nen them mot mon 3 tin
    # nua se len 20.
    registered = (
        make_registered(course_code="MAT1042", credits=4, day_of_week=2, start_period=4, end_period=6),
        make_registered(course_code="MAT1104", credits=3, day_of_week=6, start_period=7, end_period=9),
        make_registered(course_code="INT2010", credits=4, day_of_week=4, start_period=1, end_period=3),
        make_registered(course_code="INT2204", credits=3, day_of_week=5, start_period=4, end_period=6),
        make_registered(course_code="INT2011", credits=3, day_of_week=6, start_period=4, end_period=6),
    )
    context = make_context(registered=registered, max_credits=18, academic_status="canh_bao_1")
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context)
    assert not decision.allowed
    assert "vuot tran 18 tin chi" in decision.note
    assert "canh_bao_1" in decision.note


def test_landing_exactly_on_the_ceiling_is_allowed():
    # 21 credits already registered plus a 3-credit course is exactly 24: on the ceiling, not
    # over it. An off-by-one here would refuse a registration the regulation permits.
    # 21 tin da dang ky cong mot mon 3 tin la dung 24: bang tran chu khong vuot tran. Mot loi
    # lech mot don vi o day se tu choi mot lenh dang ky ma quy che cho phep.
    registered = (
        make_registered(course_code="INT2207", credits=21, day_of_week=7, start_period=1, end_period=3),
    )
    context = make_context(registered=registered, max_credits=24)
    assert check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context).allowed


def test_timetable_clash_is_refused():
    # Target is Tuesday periods 1-3; the registered class is Tuesday periods 2-4, so they
    # share periods 2 and 3.
    # Lop dang xet hoc thu 3 tiet 1-3; lop da dang ky hoc thu 3 tiet 2-4, nen hai lop trung
    # tiet 2 va tiet 3.
    registered = (make_registered(course_code="INT3405", day_of_week=3, start_period=2, end_period=4),)
    decision = check_tool_call(
        "dang_ky_hoc_phan", {"ma_lop": 1}, make_context(registered=registered)
    )
    assert not decision.allowed
    assert "trung lich" in decision.note


def test_clash_message_names_both_timetables():
    # The refusal is read out to the student by the model, so an ambiguous one becomes a wrong
    # answer. Mentioning a single time slot right after the other course's name reads as if it
    # were that course's slot, and the model duly repeats the mix-up.
    # Loi tu choi se duoc model doc lai cho sinh vien nghe, nen mot cau mo ho se bien thanh mot
    # cau tra loi sai. Neu chi neu mot khung gio ngay sau ten mon kia, cau van doc ra thanh
    # khung gio cua mon do, va model se lap lai y nguyen su nham lan ay.
    registered = (make_registered(course_code="INT2207", day_of_week=3, start_period=2, end_period=4),)
    decision = check_tool_call(
        "dang_ky_hoc_phan", {"ma_lop": 1}, make_context(registered=registered)
    )
    assert not decision.allowed
    # The class being asked for: Tuesday 1-3. The class already registered: Tuesday 2-4.
    # Lop dang xin dang ky: thu 3 tiet 1-3. Lop da dang ky: thu 3 tiet 2-4.
    assert "tiet 1-3" in decision.note
    assert "tiet 2-4" in decision.note


def test_touching_a_single_period_is_already_a_clash():
    # Target Tuesday 1-3, registered Tuesday 3-5: they overlap on period 3 alone, and that is
    # enough. This is the boundary the overlap test has to get right.
    # Lop dang xet thu 3 tiet 1-3, lop da dang ky thu 3 tiet 3-5: chung chi giao nhau dung tiet
    # 3, va the la du. Day chinh la ranh gioi ma phep kiem tra giao nhau phai bat dung.
    registered = (make_registered(course_code="INT3405", day_of_week=3, start_period=3, end_period=5),)
    decision = check_tool_call(
        "dang_ky_hoc_phan", {"ma_lop": 1}, make_context(registered=registered)
    )
    assert not decision.allowed
    assert "trung lich" in decision.note


def test_same_weekday_without_overlapping_periods_is_fine():
    # Target Tuesday 1-3, registered Tuesday 4-6: same day, no shared period, no clash.
    # Lop dang xet thu 3 tiet 1-3, lop da dang ky thu 3 tiet 4-6: cung thu, khong chung tiet
    # nao, khong trung lich.
    registered = (make_registered(course_code="INT3405", day_of_week=3, start_period=4, end_period=6),)
    assert check_tool_call(
        "dang_ky_hoc_phan", {"ma_lop": 1}, make_context(registered=registered)
    ).allowed


def test_full_class_is_refused():
    context = make_context(target=make_section(capacity=50, enrolled=50))
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context)
    assert not decision.allowed
    assert "da du si so" in decision.note


# Confirming a registration
# Xac nhan mot lenh dang ky


def test_confirming_without_a_slip_is_refused():
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DKFAKE"}, make_context())
    assert not decision.allowed
    assert "Khong tim thay phieu" in decision.note


def test_confirming_someone_elses_slip_is_refused():
    context = make_context(pending=make_pending(student_id="22021001"))
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "khong thuoc ve sinh vien" in decision.note


def test_confirming_a_slip_from_another_session_is_refused():
    context = make_context(pending=make_pending(session_id="s2"))
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed


def test_confirming_an_already_executed_slip_is_refused():
    context = make_context(pending=make_pending(status="da_thuc_hien"))
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "da duoc thuc hien roi" in decision.note


def test_confirming_an_expired_slip_is_refused():
    context = make_context(pending=make_pending(expired=True))
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "het han" in decision.note


def test_confirming_in_the_same_turn_that_created_the_slip_is_refused():
    # The rule that makes consent real. The model can prepare a registration and then, in the
    # same breath, decide the student agreed. But it cannot send a message on the student's
    # behalf, and a slip created this turn can only be confirmed by a later one.
    # Quy tac bien su dong y thanh that. Model co the tao lenh dang ky roi ngay trong cung mot
    # hoi tu ket luan rang sinh vien da dong y. Nhung no khong the gui tin nhan thay sinh vien,
    # va mot phieu tao ra trong luot nay chi co the duoc xac nhan boi mot luot sau do.
    context = make_context(pending=make_pending(created_turn_id=TURN_NOW))
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "trong chinh luot nay" in decision.note


def test_confirming_a_slip_from_an_earlier_turn_is_allowed():
    context = make_context(pending=make_pending(created_turn_id=TURN_EARLIER))
    assert check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context).allowed


def test_rules_are_rechecked_at_confirmation_time():
    # The slip was valid when it was written. Between that turn and this one the student
    # registered for another class and is now over the ceiling, so the confirmation must fail
    # even though the slip itself is in order. Checking only at preparation time would let two
    # slips prepared side by side both be confirmed and take the student past the limit.
    # Phieu nay hop le luc duoc ghi ra. Nhung giua luot do va luot nay, sinh vien da dang ky
    # them mot lop khac va gio dang vuot tran, nen lenh xac nhan phai that bai du ban than phieu
    # khong co van de gi. Neu chi kiem tra o buoc chuan bi, hai phieu duoc chuan bi song song se
    # deu duoc xac nhan va day sinh vien vuot qua gioi han.
    registered = (
        make_registered(course_code="INT3306", credits=22, day_of_week=7, start_period=1, end_period=3),
    )
    context = make_context(
        registered=registered,
        max_credits=24,
        pending=make_pending(created_turn_id=TURN_EARLIER),
    )
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "vuot tran" in decision.note


def test_confirming_into_a_class_that_filled_up_meanwhile_is_refused():
    # The class had room when the slip was written and is full by the time it is confirmed.
    # Luc ghi phieu thi lop con cho, den luc xac nhan thi lop da day.
    context = make_context(
        target=make_section(capacity=50, enrolled=50),
        pending=make_pending(created_turn_id=TURN_EARLIER),
    )
    decision = check_tool_call("xac_nhan_dang_ky", {"ma_phieu": "DK1A2B"}, context)
    assert not decision.allowed
    assert "da du si so" in decision.note


# Masking
# Che thong tin


def test_student_id_is_masked_to_the_last_four_characters():
    assert mask_student_id("22021001") == "****1001"
    assert mask_student_id("1234") == "1234"
    assert mask_student_id("") == ""
