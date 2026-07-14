[← Trang trước: Tổng quan và kiến trúc](01-tong-quan-va-kien-truc.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [RAG pipeline →](03-rag-pipeline.md)

---

# 2. Dữ liệu và lược đồ

## Các bảng

```
students              ma sinh vien, ho ten, GPA, tin chi tich luy, tinh trang hoc vu
courses               ma hoc phan, ten, so tin chi, bat buoc hay tu chon
prerequisites         (mon, mon tien quyet)  -  quan he nhieu-nhieu
class_sections        lop hoc phan: si so, suc chua, lich hoc, phong
grades                bang diem: diem so, dat hay khong dat
enrollments           cac lop sinh vien thuc su duoc ghi danh
pending_registrations phieu dang ky da tao nhung chua xac nhan
registration_windows  khoang thoi gian mo dang ky cua tung ky
documents, chunks     kho tri thuc + vector embedding
messages              lich su hoi thoai
tool_audit_log        nhat ky moi lan goi tool
```

## Ba ràng buộc đáng nói

Không phải mọi ràng buộc đều ngang nhau. Ba cái dưới đây tồn tại vì một lý do cụ thể, không phải vì thói quen.

### `CHECK (enrolled >= 0 AND enrolled <= capacity)`

Đây là **hàng rào cuối cùng**, nằm dưới toàn bộ logic ứng dụng. Nếu một lần refactor nào đó sau này làm mất cái khóa, hoặc đếm sai, thì PostgreSQL vẫn không cho lớp chứa nhiều sinh viên hơn số chỗ nó có.

Ràng buộc này không phải để phòng hờ cho vui. Nó **đã được đo là có tác dụng**: xem [trang 6](06-dong-thoi-va-transaction.md), nơi bỏ khóa đi thì chính ràng buộc này là thứ giữ được phòng tuyến.

### `UNIQUE (student_id, course_code, semester)` trên `enrollments`

Không thể đăng ký hai lớp khác nhau của cùng một môn trong một học kỳ.

Cột `course_code` trong `enrollments` là dữ liệu lặp lại (nó suy ra được từ `class_section_id`), và lặp lại như vậy thường là mùi code xấu. Ở đây nó tồn tại **chỉ để ràng buộc UNIQUE trên tồn tại được**. Đăng ký trùng môn là một lỗi mà database nên tự từ chối, chứ không phải một thứ mà ứng dụng phải nhớ để kiểm tra.

Đây là một đánh đổi có chủ đích: chấp nhận một chút dư thừa dữ liệu để đổi lấy một bất biến được database bảo đảm.

### `created_turn_id` trên `pending_registrations`

Cột này ghi lại **lượt hội thoại nào đã tạo ra phiếu đăng ký**. Guardrail từ chối thực thi một phiếu được tạo ra trong chính lượt đang chạy.

Đây là cột quan trọng nhất trong toàn bộ lược đồ, và nó là thứ biến "sinh viên đã đồng ý" từ một lời khai thành một sự thật kiểm tra được. Giải thích đầy đủ ở [trang 5](05-guardrail.md).

## Quy tắc thang điểm chỉ nằm ở một chỗ

`app/grading.py` chứa toàn bộ quy tắc: quy đổi thang 10 sang thang 4, ngưỡng đạt môn (4,0), cách tính GPA có trọng số.

Module này được dùng bởi **cả hai phía**:

- `scripts/init_db.py` dùng nó để tính GPA và tín chỉ tích lũy khi seed dữ liệu.
- `app/agent/tools.py` dùng nó cho tool `tinh_gpa_du_kien`.

Và các con số trong đó **phải khớp** với `data/documents/quy-che-dao-tao.md`, là tài liệu mà trợ lý trích dẫn cho sinh viên.

Nếu để chúng lệch nhau, hậu quả rất khó chịu: trợ lý sẽ đọc cho sinh viên nghe một quy tắc từ quy chế, rồi lại tính toán bằng một quy tắc khác từ code. Câu trả lời trôi chảy, có trích nguồn đàng hoàng, và sai.

Vì vậy GPA của sinh viên trong dữ liệu mẫu **không được ghi cứng**. Nó được tính từ bảng điểm bằng chính `grading.py`, nên ba con số (bảng điểm, GPA, tín chỉ tích lũy) không bao giờ có thể mâu thuẫn với nhau.

## Dữ liệu mô phỏng

Ba sinh viên, mỗi người tồn tại để làm **một luật khác nhau** kích hoạt. Đây không phải dữ liệu ngẫu nhiên cho có.

| Mã | Tên | Tình trạng | Vai trò |
|---|---|---|---|
| 22021001 | Nguyễn Văn An | GPA 2,84 - bình thường | **Trượt Toán rời rạc (3,5 điểm)**, mà Toán rời rạc là tiên quyết của Trí tuệ nhân tạo |
| 22021002 | Trần Thị Bình | GPA 1,35 - cảnh báo mức 1 | Trần 18 tín chỉ, **đang ở đúng 17 tín**, nên thêm bất kỳ môn 3 tín nào cũng vượt trần |
| 22021003 | Lê Minh Cường | GPA 3,73 - bình thường | Đạt hết mọi môn, nên là người **thực sự đăng ký được**, và gặp trùng lịch cùng lớp đầy |

Các lớp học phần cũng được dựng có chủ đích:

- **INT3401 nhóm 01**: Thứ 3, tiết 1-3, còn chỗ. Đây là lớp hợp lệ để Cường đăng ký.
- **INT3401 nhóm 02**: **đầy 50/50**. Đăng ký vào đây phải bị từ chối.
- **INT3405 nhóm 01**: Thứ 3, tiết **2-4**. Chồng tiết 2 và 3 với nhóm 01 ở trên, nên nếu Cường đã đăng ký INT3401 nhóm 01 thì lớp này **trùng lịch**.
- **INT3502 nhóm 01**: sức chứa 45, đã có 44. **Còn đúng một chỗ**, dùng cho bài test tranh chấp đồng thời.

Con số 17 tín chỉ của Bình là một ví dụ về việc dữ liệu mẫu phải chính xác đến từng đơn vị: nếu là 16 thì thêm một môn 3 tín sẽ thành 19, vẫn vượt trần 18 và kịch bản vẫn chạy. Nhưng 17 làm cho tình huống sát hơn và câu từ chối cụ thể hơn: *"đang ở 17 tín, thêm 3 tín nữa thành 20, vượt trần 18"*.

## Vì sao dữ liệu mô phỏng chứ không phải dữ liệu thật

Đây là câu hỏi có thể bị hỏi, nên nói thẳng: dự án không kết nối hệ thống quản lý đào tạo thật, vì không có quyền truy cập.

Nhưng điều đó **không làm giảm giá trị của phần khó**. Bài toán khó ở đây không phải là lấy được dữ liệu, mà là:

- quyết định xem model có được phép ghi hay không,
- xử lý đúng khi nhiều người cùng ghi một lúc,
- chứng minh được sau đó rằng agent đã làm đúng những gì.

Ba việc đó không dễ hơn chút nào khi dữ liệu là mô phỏng.

---

[← Trang trước: Tổng quan và kiến trúc](01-tong-quan-va-kien-truc.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [RAG pipeline →](03-rag-pipeline.md)
