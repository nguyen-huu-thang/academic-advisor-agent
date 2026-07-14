[← Trang trước: Dữ liệu và lược đồ](02-du-lieu-va-luoc-do.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Agent loop →](04-agent-loop.md)

---

# 3. RAG pipeline

## Vì sao cần RAG

Nếu để model tự trả lời câu "điều kiện tốt nghiệp là gì", nó sẽ trả lời rất trôi chảy bằng kiến thức chung về đại học Việt Nam. Và nó sẽ **bịa**, vì mỗi trường một quy chế khác nhau.

Với một trợ lý học vụ, một câu trả lời sai về điều kiện tốt nghiệp không phải chuyện nhỏ: sinh viên có thể lỡ cả một kỳ.

Nên mọi con số và mọi quy định phải đến từ tài liệu thật của nhà trường, và câu trả lời phải ghi rõ nguồn.

## Cắt tài liệu theo tiêu đề

Quy chế đào tạo là loại văn bản chia mục rất rõ. Vì vậy `app/rag/chunker.py` cắt theo tiêu đề cấp 2 và cấp 3, thay vì cắt theo số ký tự cố định.

Lý do: cắt theo số ký tự sẽ cắt ngang giữa câu, và một đoạn nói nửa về trần tín chỉ nửa về cảnh báo học vụ thì không trả lời trọn vẹn được câu hỏi nào cả. Cắt theo tiêu đề thì mỗi đoạn nói về **đúng một chủ đề**.

Đoạn nào vẫn quá dài (trên 1200 ký tự) thì mới cắt tiếp theo đoạn văn, và một đoạn văn dài hơn giới hạn thì để nguyên chứ không cắt giữa câu.

Ba tài liệu cho ra **25 đoạn**:

| Tài liệu | Số đoạn |
|---|---|
| `quy-che-dao-tao.md` | 10 |
| `huong-dan-dang-ky-hoc-phan.md` | 8 |
| `chuong-trinh-dao-tao.md` | 7 |

## Embedding và tìm kiếm

Sinh embedding bằng `gemini-embedding-001`, 768 chiều. Vector được **chuẩn hóa L2 ngay lúc lưu**, nên khi tìm kiếm chỉ cần tích vô hướng là ra độ tương đồng cosine, không phải chia cho độ dài vector mỗi lần truy vấn.

Toàn bộ 25 vector được nạp vào bộ nhớ lúc service khởi động và quét bằng numpy.

## Vì sao không dùng pgvector hay Milvus

Đây là câu hỏi gần như chắc chắn sẽ bị hỏi, nên câu trả lời phải rõ ràng.

Với 25 đoạn, một phép nhân ma trận numpy mất chưa tới một phần nghìn giây. Trong khi đó, một lời gọi Gemini mất khoảng **2000 mili giây**. Tầng truy xuất chiếm chưa tới 0,05% độ trễ của một request.

Thêm pgvector hay Milvus vào lúc này sẽ:

- thêm một thành phần phải cài đặt, cấu hình và vận hành,
- thêm một chỉ mục phải xây lại mỗi lần nạp tài liệu,
- và **không giảm được một mili giây nào** mà người dùng cảm nhận được.

Đó là chi phí thật đổi lấy lợi ích bằng không.

**Đánh đổi này thay đổi khi nào.** Quét toàn bộ có độ phức tạp tuyến tính theo số đoạn. Ở mức vài nghìn đoạn thì vẫn ổn. Vượt khoảng 100 nghìn đoạn thì thời gian quét bắt đầu đáng kể so với lời gọi LLM, và lúc đó chỉ mục xấp xỉ mới bắt đầu trả công cho chi phí vận hành của nó. Ranh giới đó là lý do để chuyển, chứ không phải vì "pgvector nghe hiện đại hơn".

Đây là một quyết định kiến trúc chủ động, không phải một sự thiếu sót.

## Đo chất lượng truy xuất

Nói "dịch vụ có RAG" thì chưa nói lên được gì.

Một pipeline lấy về **sai** đoạn văn vẫn sinh ra câu trả lời trôi chảy, tự tin, và sai. Tệ hơn nữa: dòng trích nguồn ở cuối làm nó trông **đáng tin hơn**, chứ không phải kém tin đi. Sinh viên đọc thấy "Nguồn: Quy chế đào tạo đại học 2026" thì sẽ tin.

Nên bộ tìm kiếm được chấm điểm trên **32 câu hỏi đã biết trước đoạn văn đúng** (`scripts/eval_rag.py`):

| Chỉ số | Giá trị | Ý nghĩa |
|---|---|---|
| Recall@1 | 78,1% (25/32) | Đoạn đúng đứng đầu bảng |
| Recall@4 | 96,9% (31/32) | Đoạn đúng nằm trong 4 kết quả trả về cho model |
| MRR | 0,846 | Trung bình của 1 chia cho thứ hạng của đoạn đúng |

**Vì sao đo cả hai.** Recall@4 là con số quan trọng nhất về mặt vận hành: nếu đoạn đúng không lọt vào top 4 thì nó **không bao giờ đến được tay model**, và model chỉ còn cách đoán. Nhưng Recall@4 không phân biệt được "đoạn đúng đứng đầu" với "đoạn đúng nằm thứ tư". MRR phân biệt được, và nó cho biết đoạn nhiễu đang chen vào nhiều hay ít.

## Câu duy nhất trượt

Trung thực để lại trong kết quả, không giấu:

> **Câu hỏi:** "Giải tích 1 bao nhiêu tín chỉ?"
> **Cần lấy:** mục "Khối kiến thức toán và khoa học cơ bản"
> **Lấy nhầm:** phần mở đầu của Chương trình đào tạo

**Nguyên nhân:** phần mở đầu của tài liệu không có tiêu đề cấp 2, nên nó đứng thành một đoạn riêng. Đoạn đó chứa câu "tổng khối lượng 130 tín chỉ", và về mặt ngữ nghĩa thì nó gần với câu hỏi chứa chữ "tín chỉ" hơn là mục liệt kê từng môn.

**Cách sửa nếu cần:** gắn tiêu đề của tài liệu vào đầu mỗi đoạn khi sinh embedding, để đoạn liệt kê môn học mang theo ngữ cảnh "chương trình đào tạo". Hoặc đơn giản hơn: bỏ hẳn đoạn mở đầu ra khỏi kho tri thức, vì nó không chứa thông tin nào trả lời được câu hỏi cụ thể.

Chưa sửa vì Recall@4 đã 96,9%, và câu này vẫn được trả lời đúng nhờ model đọc được số tín chỉ từ tool `tim_lop_hoc_phan`. Nhưng nó là một điểm yếu có thật, và biết rõ nó ở đâu thì tốt hơn là không biết.

## Chống bịa số liệu

Ba lớp cùng lúc:

1. **System instruction** bắt buộc mọi con số về điểm, GPA, tín chỉ, sĩ số, học phí phải đến từ kết quả tool, và câu trả lời phải ghi rõ nguồn.
2. **Tool trả về "không tìm thấy"** thay vì trả về rỗng, kèm ghi chú *"Hãy nói rõ là không có thông tin"*.
3. **Quy tắc thang điểm chỉ nằm ở một chỗ** (`grading.py`), nên con số trong quy chế và con số dùng để tính toán không thể lệch nhau.

Kịch bản kiểm chứng trong demo: hỏi *"Trường có cấp học bổng du học Nhật Bản không?"*. Không có tài liệu nào nói về việc này, và trợ lý phải nói thẳng là không có thông tin, chứ không được bịa ra một chương trình học bổng nghe rất hợp lý.

---

[← Trang trước: Dữ liệu và lược đồ](02-du-lieu-va-luoc-do.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Agent loop →](04-agent-loop.md)
