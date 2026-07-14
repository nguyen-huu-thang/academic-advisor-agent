[← Trang trước: RAG pipeline](03-rag-pipeline.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Guardrail →](05-guardrail.md)

---

# 4. Agent loop

## Vòng lặp

```python
while iterations < max_tool_iterations:      # tối đa 5
    iterations += 1
    result = client.generate(contents, system_instruction, tools)

    if not result.function_calls:            # model đã có câu trả lời
        answer = result.text
        break

    contents.append(result.content)          # ghi lại ý định của model

    for call in result.function_calls:
        record, payload = handle_tool_call(base_context, call)
        response_parts.append(function_response(call.name, payload))

    contents.append(Content(role="user", parts=response_parts))
else:
    answer = "Yêu cầu này cần nhiều bước tra cứu hơn mức cho phép..."
```

Đây là mô hình **Planning - Tool Use - Memory - Execution** kinh điển: model tự lập kế hoạch, tự chọn tool, quan sát kết quả, rồi quyết định bước tiếp theo.

## Vì sao tự viết vòng lặp thay vì dùng của SDK

Gemini SDK có sẵn `automatic_function_calling`: đưa tool vào, nó tự gọi, tự lặp, trả về câu trả lời cuối. Rất tiện.

Nhưng nó bị **tắt** trong dự án này:

```python
automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
```

Lý do: nếu SDK tự gọi tool, thì **không có chỗ nào để chen guardrail và audit log vào giữa**. Lời gọi tool sẽ đi thẳng từ model xuống hàm Python, không qua ai kiểm duyệt.

Cả hai thứ đáng giá nhất của dự án đều nằm ở khoảng giữa đó. Nên khoảng giữa đó phải là của mình.

Đây là một đánh đổi rõ ràng: viết thêm khoảng 40 dòng vòng lặp, đổi lấy quyền kiểm soát mọi lời gọi tool.

## Vì sao giới hạn 5 vòng

Hai lý do, và lý do thứ hai ít người nghĩ tới.

**An toàn.** Một model cứ liên tục đòi gọi tool mà không bao giờ đưa ra câu trả lời sẽ chạy mãi. Vòng lặp có trần thì tệ nhất cũng chỉ tốn 5 lời gọi rồi dừng, và trả về một câu xin lỗi tử tế.

**Chi phí.** Đây mới là lý do thú vị. Số liệu đo được trên 10 request:

| Chỉ số | Giá trị |
|---|---|
| Token vào | 38.025 |
| Token ra | 1.549 |
| **Tỷ lệ vào / ra** | **~25 lần** |

Với một agent, hóa đơn nằm gần như trọn vẹn ở **phía đầu vào**. Và đầu vào phình lên theo từng vòng lặp, vì mỗi vòng lại nối thêm toàn bộ kết quả tool vào hội thoại rồi **gửi lại từ đầu**.

Nghĩa là: vòng lặp thứ ba đắt hơn vòng lặp thứ nhất, dù cùng gọi một model. Giới hạn 5 vòng vì vậy không chỉ là biện pháp an toàn, mà còn là biện pháp kiểm soát chi phí.

Thực tế đo được: mỗi câu hỏi tốn **2 đến 3 vòng**, nên trần 5 là rộng rãi chứ không bó buộc.

## TurnContext: đọc dữ liệu trước khi model nói

Đây là chi tiết thiết kế quan trọng nhất của vòng lặp.

Trước khi model được hỏi bất cứ điều gì, `_load_turn_context` đọc từ database:

```python
TurnContext(
    student_id      = ...,   # từ tầng xác thực, không từ model
    session_id      = ...,
    turn_id         = uuid4().hex,          # mới cho mỗi tin nhắn của sinh viên
    semester        = ...,
    academic_status = ...,                  # từ bảng students
    max_credits     = ...,                  # suy từ academic_status
    registration_open = ...,                # do đồng hồ của PostgreSQL quyết định
    passed_courses  = frozenset(...),       # từ bảng grades
    registered      = (...),                # từ bảng enrollments
)
```

**Vì sao phải đọc trước.** Nếu đọc sau khi model đã nói, thì hóa ra lại đọc một thế giới mà model đã kịp mô tả lại. Đọc trước thì dữ liệu là dữ liệu, không phải là thứ model đã nhúng tay vào.

**`turn_id` là gì.** Một mã mới sinh cho **đúng một tin nhắn** của sinh viên. Nó là thứ cho phép guardrail phân biệt "sinh viên đã trả lời tôi" với "model tự quyết định rằng sinh viên chắc sẽ trả lời như vậy". Chi tiết ở [trang 5](05-guardrail.md).

Khi một lời gọi tool cụ thể cần thêm dữ liệu (lớp nào, phiếu nào), `_context_for_call` gắn thêm vào bằng `dataclasses.replace`:

```python
if tool_name == "xac_nhan_dang_ky":
    pending = load_pending_registration(slip_id)
    target  = load_class_section(pending.class_section_id)   # lớp lấy từ PHIẾU
    return replace(base, pending=pending, target=target)
```

Chú ý dòng giữa: khi xác nhận, lớp học phần được lấy **từ phiếu**, không phải từ tham số của model. Model chỉ đưa ra một mã phiếu và không đưa gì khác. Nhờ vậy nó **không thể** chuẩn bị đăng ký cho lớp này rồi xác nhận để chui vào lớp khác.

## Bộ nhớ hội thoại

Lịch sử được lưu trong PostgreSQL, và chỉ **10 lượt gần nhất** được phát lại cho model.

Vì sao cắt: gửi toàn bộ lịch sử sẽ làm prompt phình ra vô hạn, mà input token chính là thứ tạo nên hóa đơn (xem tỷ lệ 25 lần ở trên).

**Một chi tiết bảo mật nhỏ.** Câu truy vấn lịch sử dùng **cả** `session_id` **và** `student_id`:

```sql
WHERE session_id = %s AND student_id = %s
```

Nếu chỉ cần `session_id` là lấy được hội thoại, thì đoán trúng hoặc dùng lại session id của người khác sẽ kéo hội thoại của họ vào prompt của sinh viên này. Bắt buộc có cả mã sinh viên nghĩa là một session id của người khác chỉ trả về rỗng.

## Xử lý lỗi từ nhà cung cấp

Gemini trả **429** khi hết quota và **503** khi model quá tải. Cả hai đều đáng thử lại; còn 400 hay 404 là lỗi của chính mình, thử lại chỉ tốn thời gian.

Cách xử lý:

- **Exponential backoff kèm jitter.** Jitter để nhiều request cùng bị từ chối một lúc không cùng thức dậy đồng loạt rồi lại đâm vào quota một lần nữa.
- **Tôn trọng khoảng chờ Gemini đề nghị, nhưng chặn trên ở 8 giây.** Gemini đôi khi bảo "chờ 51 giây". Một sinh viên đang ngồi chờ một request HTTP sẽ không đợi trọn 51 giây đó.
- **Hết số lần thử lại thì trả HTTP 429 kèm header `Retry-After`**, không phải 500.

Điểm cuối cùng là một quyết định có ý nghĩa: **bị nhà cung cấp chặn không phải là lỗi nội bộ của dịch vụ**. Trả 500 sẽ khiến người vận hành đi tìm bug trong code, trong khi việc cần làm là chờ hoặc nâng quota. Mã lỗi phải nói đúng sự thật về chuyện gì đang xảy ra.

---

[← Trang trước: RAG pipeline](03-rag-pipeline.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Guardrail →](05-guardrail.md)
