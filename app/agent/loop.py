"""The agent loop: plan, call tools, observe results, answer.

Vòng lặp agent: lập kế hoạch, gọi tool, quan sát kết quả, trả lời.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, replace

from google.genai import types

from app.agent.guardrail import TurnContext, check_tool_call, mask_student_id
from app.agent.tools import (
    GEMINI_TOOLS,
    ToolExecutor,
    is_registration_open,
    load_class_section,
    load_passed_courses,
    load_pending_registration,
    load_registered,
    load_student,
)
from app.config import Settings
from app.db import get_connection
from app.llm.gemini import GeminiClient, Usage
from app.memory.conversation import ConversationMemory

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """Ban la tro ly co van hoc tap cua mot truong dai hoc Viet Nam, tra loi bang tieng Viet.

Nguyen tac bat buoc:
1. Khong bao gio tu bia so lieu. Moi con so ve diem, GPA, tin chi, si so lop, hoc phi, cung
   moi quy dinh cua nha truong, deu phai lay tu ket qua tool. Neu tool khong tra ve du lieu,
   hay noi thang la ban khong co thong tin, tuyet doi khong doan.
2. Khi tra loi cau hoi ve quy che, dieu kien tot nghiep, canh bao hoc vu, tran tin chi hay
   hoc phi, phai goi tool tim_kiem_quy_che truoc, va ghi ro nguon o cuoi cau tra loi theo
   dang "Nguon: <ten tai lieu>".
3. Dang ky hoc phan gom hai buoc tach roi. Truoc het goi dang_ky_hoc_phan de tao phieu, roi
   doc lai cho sinh vien ma hoc phan, ten hoc phan, so tin chi, nhom lop, giang vien, lich
   hoc, phong hoc va ma phieu, sau do hoi sinh vien co xac nhan khong va DUNG LAI cho sinh
   vien tra loi. Chi khi sinh vien tra loi dong y trong mot tin nhan moi, ban moi duoc goi
   xac_nhan_dang_ky. Tuyet doi khong goi xac_nhan_dang_ky trong cung mot luot voi
   dang_ky_hoc_phan, du sinh vien co noi truoc la ho dong y.
4. Neu mot yeu cau dang ky bi tu choi, hay giai thich ro ly do cho sinh vien va goi y huong
   xu ly, vi du hoc lai mon tien quyet, chon lop khac, hoac bo bot mon de khong vuot tran.
5. Chi tra loi trong pham vi hoc vu va dang ky hoc phan. Neu duoc hoi chuyen khac, tu choi
   lich su va huong sinh vien quay lai chu de hoc tap.
6. Tra loi ngan gon, ro rang, dung dinh dang de doc.
"""


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict
    allowed: bool
    note: str | None = None


@dataclass
class AgentResult:
    answer: str
    usage: Usage
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    latency_ms: float = 0.0


class StudentNotFound(Exception):
    """The session names a student the university has no record of.

    Phiên làm việc nêu ra một sinh viên mà nhà trường không có hồ sơ.
    """


class AdvisorAgent:
    def __init__(
        self,
        client: GeminiClient,
        executor: ToolExecutor,
        memory: ConversationMemory,
        settings: Settings,
    ) -> None:
        self._client = client
        self._executor = executor
        self._memory = memory
        self._settings = settings

    def run(self, session_id: str, student_id: str, user_message: str) -> AgentResult:
        started = time.perf_counter()

        # Everything the guardrail is allowed to trust is read once, here, before the model is
        # given a chance to say anything. Reading it after the model spoke would mean reading a
        # world the model has had a chance to describe.
        # Mọi thứ guardrail được phép tin đều được đọc một lần, tại đây, trước khi model có cơ
        # hội nói bất cứ điều gì. Nếu đọc sau khi model đã nói thì hóa ra lại đọc một thế giới mà
        # model đã kịp mô tả lại.
        base_context = self._load_turn_context(session_id, student_id)

        contents = self._memory.load_history(session_id, student_id)
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=user_message)])
        )

        usage = Usage()
        tool_calls: list[ToolCallRecord] = []
        answer = ""
        iterations = 0

        # The loop is bounded: a model that keeps asking for tools forever would otherwise burn
        # tokens and money without ever answering the student.
        # Vòng lặp có giới hạn: nếu không, một model cứ liên tục đòi gọi tool sẽ đốt token và
        # tiền mà không bao giờ trả lời sinh viên.
        while iterations < self._settings.max_tool_iterations:
            iterations += 1
            result = self._client.generate(
                contents,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=GEMINI_TOOLS,
            )
            usage.add(result.usage)

            # No function call in the reply means the model is done looking things up and has
            # produced its final answer for the student.
            # Không còn lệnh gọi hàm trong phản hồi nghĩa là model đã tra cứu xong và đưa ra
            # câu trả lời cuối cùng cho sinh viên.
            if not result.function_calls:
                answer = result.text or "Xin loi, toi chua tao duoc cau tra loi."
                break

            if result.content is not None:
                contents.append(result.content)

            # Each requested call is checked and executed one by one; every result is sent
            # back to the model in the next round so it can keep reasoning.
            # Từng lệnh gọi được kiểm tra và thực thi lần lượt; mọi kết quả được gửi ngược lại
            # cho model ở vòng kế tiếp để nó tiếp tục suy luận.
            response_parts = []
            for call in result.function_calls:
                record, payload = self._handle_tool_call(base_context, call)
                tool_calls.append(record)
                response_parts.append(
                    types.Part.from_function_response(name=call.name, response=payload)
                )

            contents.append(types.Content(role="user", parts=response_parts))
        else:
            # The loop ran out of iterations without the model producing an answer.
            # Vòng lặp hết số lần cho phép mà model vẫn chưa đưa ra câu trả lời.
            answer = (
                "Xin loi, yeu cau nay can nhieu buoc tra cuu hon muc cho phep. "
                "Ban vui long chia nho cau hoi giup toi."
            )

        self._memory.append(session_id, student_id, "user", user_message)
        self._memory.append(session_id, student_id, "model", answer)

        return AgentResult(
            answer=answer,
            usage=usage,
            tool_calls=tool_calls,
            iterations=iterations,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def _load_turn_context(self, session_id: str, student_id: str) -> TurnContext:
        student = load_student(student_id)
        if student is None:
            raise StudentNotFound(f"Khong tim thay sinh vien {mask_student_id(student_id)}.")

        semester = self._settings.current_semester
        academic_status = student["academic_status"]

        return TurnContext(
            student_id=student_id,
            session_id=session_id,
            # A fresh id for this one student message. It is what lets the guardrail tell "the
            # student replied to me" apart from "the model decided the student would have replied".
            # Một id mới cho đúng một tin nhắn này của sinh viên. Chính nó cho phép guardrail phân
            # biệt "sinh viên đã trả lời tôi" với "model tự quyết định rằng sinh viên chắc sẽ
            # trả lời như vậy".
            turn_id=uuid.uuid4().hex,
            semester=semester,
            academic_status=academic_status,
            max_credits=self._settings.max_credits_for(academic_status),
            registration_open=is_registration_open(semester),
            passed_courses=load_passed_courses(student_id),
            registered=load_registered(student_id, semester),
        )

    def _context_for_call(
        self, base: TurnContext, tool_name: str, arguments: dict
    ) -> TurnContext:
        """Attach the class, and the slip, that this particular call is about.

        Gắn vào context đúng lớp học phần, và đúng phiếu, mà lời gọi cụ thể này đang nhắc tới.

        Note where the class comes from when confirming: from the slip, not from the model's
        arguments. The model hands over a slip code and nothing else, so it cannot prepare a
        registration for one class and then confirm its way into another.
        Chú ý lớp học phần đến từ đâu khi xác nhận: đến từ phiếu, không đến từ tham số của model.
        Model chỉ đưa ra một mã phiếu chứ không đưa gì khác, nên nó không thể chuẩn bị một lệnh
        đăng ký cho lớp này rồi xác nhận để chui vào một lớp khác.
        """
        if tool_name == "dang_ky_hoc_phan":
            section_id = _as_int(arguments.get("ma_lop"))
            if section_id is None:
                return replace(base, target=None)
            return replace(base, target=load_class_section(section_id, base.semester))

        if tool_name == "xac_nhan_dang_ky":
            slip_id = str(arguments.get("ma_phieu", "")).strip()
            pending = load_pending_registration(slip_id)
            if pending is None:
                return replace(base, pending=None, target=None)
            target = load_class_section(pending.class_section_id, base.semester)
            return replace(base, pending=pending, target=target)

        return base

    def _handle_tool_call(
        self, base_context: TurnContext, call: types.FunctionCall
    ) -> tuple[ToolCallRecord, dict]:
        """Run one tool call through the guardrail, the audit log, and then execution.

        Cho một lệnh gọi tool đi qua guardrail, ghi nhật ký kiểm toán, rồi mới thực thi.
        """
        name = call.name or ""
        arguments = dict(call.args or {})

        context = self._context_for_call(base_context, name, arguments)
        decision = check_tool_call(name, arguments, context)
        self._write_audit_log(context, name, arguments, decision.allowed, decision.note)

        logger.info(
            "student=%s tool=%s allowed=%s args=%s",
            mask_student_id(context.student_id),
            name,
            decision.allowed,
            json.dumps(arguments, ensure_ascii=False),
        )

        record = ToolCallRecord(
            name=name, arguments=arguments, allowed=decision.allowed, note=decision.note
        )

        if not decision.allowed:
            # The refusal is fed back to the model as the tool's result, so it can explain the
            # situation to the student instead of silently failing.
            # Lý do từ chối được trả ngược lại cho model như kết quả của tool, để model giải thích
            # lại cho sinh viên thay vì thất bại âm thầm.
            return record, {"tu_choi": decision.note}

        try:
            payload = self._executor.execute(name, arguments, context)
        except Exception:
            logger.exception("Tool %s that bai", name)
            payload = {"loi": "Tool gap loi khi thuc thi. Hay bao sinh vien thu lai sau."}

        return record, payload

    @staticmethod
    def _write_audit_log(
        context: TurnContext,
        tool_name: str,
        arguments: dict,
        allowed: bool,
        note: str | None,
    ) -> None:
        """Record the call and the verdict before the tool runs, not after.

        Ghi lại lệnh gọi và quyết định trước khi tool chạy, chứ không phải sau.

        Writing afterwards would lose exactly the calls worth investigating: the ones that
        crashed halfway through.
        Nếu ghi sau khi chạy xong thì sẽ mất đúng những lệnh gọi đáng để điều tra nhất: những
        lệnh vỡ giữa chừng.
        """
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO tool_audit_log
                    (session_id, student_id, tool_name, arguments, allowed, denial_note)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    context.session_id,
                    context.student_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    allowed,
                    note,
                ),
            )


def _as_int(value: object) -> int | None:
    """Read a class id the model supplied, without trusting it to be a number.

    Đọc mã lớp do model đưa ra, mà không tin rằng nó chắc chắn là một con số.
    """
    # bool is a subclass of int in Python, so it must be rejected first.
    # bool là lớp con của int trong Python, nên phải loại nó trước tiên.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
