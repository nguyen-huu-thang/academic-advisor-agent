[← Trang trước: Đồng thời và transaction](06-dong-thoi-va-transaction.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Kiểm thử →](08-kiem-thu.md)

---

# 7. Đo lường và chi phí

## Vì sao đo chi phí song song với độ trễ

Với một dịch vụ web thông thường, chỉ số vận hành quan trọng là độ trễ và tỷ lệ lỗi.

Với một dịch vụ LLM, có thêm một chỉ số quan trọng không kém: **số tiền tiêu cho mỗi request**.

Lý do: chi phí của một dịch vụ LLM tăng tuyến tính theo lưu lượng, và nó tăng ngay lập tức. Một vòng lặp agent chạy dư một vòng không làm dịch vụ chậm đi đáng kể, nhưng nó làm hóa đơn cuối tháng phình lên theo đúng số request. Nếu không đo thì không biết, và đến khi biết thì đã tiêu mất tiền rồi.

Vì vậy `/metrics` xuất chi phí như một counter Prometheus, ngang hàng với độ trễ:

```
agent_requests_total          So request da xu ly
agent_errors_total            So request bi loi
agent_tool_denied_total       So lan guardrail chan mot lenh goi tool
agent_refresh_reuse_total     So lan mot refresh token da dung bi trinh ra lai
agent_tokens_total{direction} Token vao / token ra
agent_cost_usd_total          Chi phi uoc tinh (USD)
agent_latency_ms{quantile}    p50 / p95 / p99
agent_tool_calls_total{tool}  So lan tung tool duoc goi
```

Hai chỉ số đáng chú ý ở đây là chỉ số về **an toàn**, không phải về hiệu năng.

`agent_tool_denied_total`: nếu con số này đột nhiên tăng vọt, hoặc là dữ liệu đang sai, hoặc là có ai đó đang thử tấn công hệ thống. Cả hai đều đáng biết.

`agent_refresh_reuse_total`: **bình thường nó phải bằng 0**. Một giá trị khác 0 nghĩa là hoặc một refresh token đã bị dùng lại bởi người lẽ ra không được cầm nó, hoặc một client đang gửi lại lệnh refresh sai cách. Cái thứ nhất đáng đánh thức người trực dậy; cái thứ hai là một cái bug cần sửa. Và không cái nào nhìn thấy được ở bất cứ đâu khác.

## Hai endpoint này không dành cho sinh viên

`/metrics` và `/stats` nằm sau `METRICS_TOKEN`, một khóa **khác** với token đăng nhập của sinh viên, và token của sinh viên không mở được chúng.

Lý do đơn giản: chúng báo cáo số tiền dịch vụ tiêu, số token đã dùng, và số lần guardrail phải ra tay. Đó là việc của người vận hành. Một sinh viên đăng nhập được thì không vì thế mà được đọc hóa đơn, và `agent_tool_denied_total` còn nói cho người ngoài biết chính xác khi nào các đòn tấn công của họ chạm tới guardrail - vốn là thứ cuối cùng nên đưa cho họ.

`/health` thì vẫn công khai, vì load balancer phải gọi được nó mà không cầm theo bí mật nào.

## Bộ đếm thì tích lũy, độ trễ thì trượt

`agent_requests_total`, `agent_cost_usd_total` và các counter khác đếm tích lũy từ lúc tiến trình khởi động, đúng như một counter Prometheus phải thế.

Nhưng **độ trễ chỉ giữ 10.000 mẫu gần nhất**, trong một `deque` có giới hạn.

Trước đây nó là một list không giới hạn, và đó là một cách chậm rãi để hết bộ nhớ: mỗi request thêm một số thực và không bao giờ bớt đi, nên một dịch vụ chạy đủ lâu sẽ giữ hàng triệu số, còn `np.percentile` thì chậm dần theo từng số một.

Cửa sổ có giới hạn cũng là phép đo **trung thực hơn**. Phân vi tính trên mọi request từ lúc tiến trình khởi động trả lời câu "dịch vụ này từ trước tới nay chạy ra sao", vốn không ai hỏi. Phân vị tính trên vài nghìn request gần nhất trả lời câu "nó **đang** chạy ra sao" - đúng câu mà một biểu đồ độ trễ sinh ra để trả lời, và là câu sẽ đổi ngay khi có sự cố.

## Số liệu đo được

Đo trên 10 request của `scripts/demo.py`, model `gemini-3.1-flash-lite`, PostgreSQL 18 chạy cục bộ, **service vừa khởi động lại để bộ đếm bắt đầu từ 0**:

| Chỉ số | Giá trị |
|---|---|
| Độ trễ p50 | 1.998 ms |
| Độ trễ p95 | 2.860 ms |
| Chi phí trung bình mỗi request | khoảng 0,00118 USD (~31 VND) |
| Token vào | 38.025 |
| Token ra | 1.549 |
| Số vòng lặp agent mỗi câu hỏi | 2 đến 3 |

## Một cái bẫy khi đo

Lần chạy đầu tiên cho ra 16 request và chi phí 0,0205 USD. Nhưng demo chỉ có 10 tin nhắn.

Nguyên nhân: `metrics` là bộ đếm **trong tiến trình**, tích lũy từ lúc service khởi động. Sáu request dư là các lần thử tay trước đó.

Nghĩa là con số đầu tiên **trộn lẫn hai lần đo khác nhau**, và nếu cứ thế đưa vào README thì nó sai. Phải khởi động lại service rồi chạy lại demo mới có số sạch.

Đây là một bài học nhỏ nhưng thật: **một chỉ số chỉ có nghĩa khi biết rõ nó đang đếm từ lúc nào.**

## Độ trễ đến từ đâu

Gần như toàn bộ độ trễ nằm ở **chính lời gọi Gemini**.

| Thành phần | Thời gian |
|---|---|
| Một lời gọi Gemini | ~700 đến 1.000 ms |
| Tìm kiếm vector trong bộ nhớ (25 đoạn) | dưới 1 ms |
| Truy vấn PostgreSQL | vài ms |

Với 2 đến 3 vòng lặp agent, tổng ra khoảng 2.000 ms.

**Hệ quả cho việc tối ưu.** Tối ưu tầng truy xuất là vô nghĩa: nó đang chiếm chưa tới 0,05% thời gian. Muốn nhanh hơn thì phải nhắm vào:

1. **Giảm số vòng lặp agent.** Mỗi vòng là một lời gọi Gemini trọn vẹn. Cách làm: mô tả tool rõ hơn để model chọn đúng ngay từ lần đầu, hoặc gộp `tim_lop_hoc_phan` và `dang_ky_hoc_phan` để bớt một vòng.
2. **Rút ngắn prompt.** Ít token vào thì model xử lý nhanh hơn và rẻ hơn.

Đây là lý do vì sao đo trước rồi mới tối ưu. Nếu không đo, rất dễ ngồi tối ưu vector search và tự hài lòng, trong khi nó không ảnh hưởng gì tới thứ người dùng cảm nhận được.

## Chi phí nằm ở đâu

Tỷ lệ token vào trên token ra là **25 lần**.

Với một agent, hóa đơn nằm gần như trọn vẹn ở **phía đầu vào**. Và đầu vào phình lên theo từng vòng lặp: mỗi vòng lại nối thêm toàn bộ kết quả tool vào hội thoại rồi **gửi lại từ đầu**.

Nghĩa là vòng lặp thứ ba đắt hơn vòng lặp thứ nhất, dù cùng gọi một model với cùng một giá.

Ba hệ quả về thiết kế:

**1. Giới hạn 5 vòng lặp là biện pháp kiểm soát chi phí, không chỉ là biện pháp an toàn.**

**2. Cắt lịch sử hội thoại ở 10 lượt.** Gửi toàn bộ lịch sử sẽ làm prompt phình vô hạn, mà input token chính là thứ tạo nên hóa đơn.

**3. Kết quả tool nên gọn.** Một tool trả về 50 dòng dữ liệu thì 50 dòng đó sẽ được gửi lại cho model ở mọi vòng lặp sau đó. `tim_lop_hoc_phan` vì vậy chỉ trả về các trường thực sự cần, không trả về nguyên cả dòng database.

## Bảng giá

`app/config.py` giữ bảng giá theo từng model, lấy từ trang giá chính thức của Gemini:

```python
PRICE_PER_1M_TOKENS = {
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50},
    ...
}
```

Nếu model không có trong bảng, `estimate_cost_usd` trả về 0 thay vì ném lỗi. Đây là quyết định có chủ đích: **không biết giá thì không được phép làm sập dịch vụ**. Một chỉ số thiếu thì tệ, nhưng một dịch vụ chết vì không tra được giá thì tệ hơn nhiều.

Đánh đổi: chi phí sẽ bị báo thiếu một cách âm thầm nếu đổi sang model mới mà quên cập nhật bảng giá. Chấp nhận được, vì bảng giá nằm ngay cạnh phần cấu hình model.

## Vì sao chặn thời gian chờ ở 8 giây

Khi hết quota, Gemini trả 429 kèm gợi ý "chờ 51 giây rồi thử lại".

Dịch vụ **không** chờ 51 giây. Nó chặn trên ở 8 giây, và nếu hết số lần thử lại thì trả HTTP 429 kèm header `Retry-After`.

Lý do: một sinh viên đang ngồi chờ một request HTTP sẽ không đợi trọn 51 giây. Họ sẽ tưởng trang web hỏng, bấm F5, và tạo thêm một request nữa vào đúng cái quota đang cạn.

Trả 429 nhanh và nói rõ "chờ bao lâu rồi quay lại" là hành vi trung thực hơn, và cũng nhẹ tải hơn cho hệ thống.

Và mã lỗi phải nói đúng sự thật: **bị nhà cung cấp chặn không phải lỗi nội bộ của dịch vụ.** Trả 500 sẽ khiến người vận hành đi tìm bug trong code, trong khi việc cần làm là chờ hoặc nâng quota.

---

[← Trang trước: Đồng thời và transaction](06-dong-thoi-va-transaction.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Kiểm thử →](08-kiem-thu.md)
