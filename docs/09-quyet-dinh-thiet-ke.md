[← Trang trước: Kiểm thử](08-kiem-thu.md) | [Mục lục](../README.md#tài-liệu-chi-tiết)

---

# 9. Quyết định thiết kế và giới hạn

Trang này gom lại các đánh đổi đã chọn, lý do chọn, và điều kiện nào sẽ làm lựa chọn đó đổi.

## Bảng tổng hợp các đánh đổi

| Quyết định | Phương án bị loại | Vì sao | Khi nào sẽ đổi |
|---|---|---|---|
| Guardrail trong code | Guardrail trong prompt | Prompt bị nói khích được, code thì không. Đã chứng minh bằng chạy thật | Không bao giờ |
| Guardrail là hàm thuần | Guardrail tự truy vấn database | 26 test chạy 0,05 giây, không cần DB, không có trạng thái ẩn | Không bao giờ |
| Tự viết agent loop | `automatic_function_calling` của SDK | SDK không để chỗ nào chen guardrail và audit log vào | Không bao giờ |
| Tách đăng ký làm 2 tool | Một tool với cờ `da_xac_nhan` | Cờ boolean chỉ là model tự khen mình. Cần bằng chứng, không cần lời khai | Không bao giờ |
| Quét vector toàn bộ bằng numpy | pgvector, Milvus | 25 đoạn quét mất dưới 1 ms, trong khi Gemini mất 2000 ms | Vượt khoảng 100 nghìn đoạn |
| Bỏ tham số mã sinh viên khỏi tool | Cho model điền rồi kiểm tra quyền | Ô trống không tồn tại thì không lạm dụng được | Khi cần phục vụ cố vấn học tập tra nhiều sinh viên |
| Bỏ luôn mã sinh viên khỏi body HTTP | Nhận trong body rồi đối chiếu với token | Cùng một lý do, áp ra một lớp nữa. Một trường không tồn tại thì không thể quên kiểm tra nó | Không bao giờ |
| scrypt cho mật khẩu | SHA-256, hay bcrypt | Hàm băm nhanh là điểm yếu, không phải ưu điểm, khi bảng bị đánh cắp. scrypt cứng cả CPU lẫn bộ nhớ và có sẵn trong thư viện chuẩn | Nếu cần chuẩn hóa theo Argon2id |
| Ghim thuật toán JWT là HS256 | Đọc thuật toán từ header của token | Token không được quyền quyết định nó sẽ bị kiểm tra ra sao. Đó là toàn bộ nội dung của hai lỗ hổng `alg: none` và alg-confusion | Không bao giờ |
| Khóa dòng lớp | Khóa cả bảng | Hai người đăng ký hai lớp khác nhau không có lý do gì phải chờ nhau | Không bao giờ |
| Chặn thời gian chờ ở 8 giây | Chờ trọn 51 giây theo Gemini | Sinh viên không đợi 51 giây, họ sẽ F5 và tạo thêm tải | Nếu chuyển sang xử lý bất đồng bộ |
| Trả 429 khi hết quota | Trả 500 | Bị nhà cung cấp chặn không phải lỗi nội bộ | Không bao giờ |

## Ba nguyên tắc rút ra

### 1. Đừng hỏi model những gì có thể tự đọc được

Model không cần nói cho ta biết sinh viên là ai, sinh viên đã học môn gì, hay sinh viên có đồng ý hay không. Cả ba thứ đó đều đọc được từ database và từ tầng xác thực.

Mỗi lần đưa một tham số cho model là mỗi lần tạo thêm một đường cho nó nói sai. Số lượng tham số model được phép điền vào các tool nên là **tối thiểu**, và mọi tham số còn lại phải chịu được việc model điền bậy.

### 2. Bằng chứng đánh bại lời khai

`da_xac_nhan = true` là một **lời khai**: model tự nói rằng sinh viên đã đồng ý.

Một dòng trong `pending_registrations` với `created_turn_id` khác lượt hiện tại là một **bằng chứng**: nó chứng tỏ sinh viên đã phải gửi thêm một tin nhắn nữa, và model không có cách nào gửi tin nhắn thay sinh viên.

Khi thiết kế một hệ thống mà một thành phần có thể nói dối (dù không cố ý), hãy tìm cách biến những khẳng định quan trọng thành thứ **kiểm tra được từ bên ngoài** thành phần đó.

### 3. Đo trước, đừng đoán

Ba lần trong dự án này, câu trả lời hiển nhiên đã sai:

- **Tưởng** rằng bỏ `FOR UPDATE` sẽ làm sĩ số lớp vượt trần. **Đo ra**: dữ liệu vẫn đúng nhờ `CHECK`, nhưng 19 sinh viên nhận lỗi 500 thay vì lời từ chối tử tế.
- **Tưởng** rằng kịch bản lớp đầy sẽ chứng minh guardrail. **Chạy ra**: model tự tránh, không gọi tool, nên guardrail không hề được thử.
- **Tưởng** rằng 16 request trong metrics là của demo. **Nhìn kỹ**: bộ đếm tích lũy từ lúc service khởi động, đã trộn lẫn với các lần thử tay trước đó.

Cả ba lần, chỉ có chạy thật mới lộ ra sự thật.

## Giới hạn đã biết

Nói thẳng, không giấu.

**Dữ liệu là mô phỏng.** Không kết nối hệ thống quản lý đào tạo thật, vì không có quyền truy cập. Nhưng phần khó của bài toán (quyết định model có được ghi hay không, xử lý đúng khi nhiều người cùng ghi, chứng minh được agent đã làm gì) không dễ đi chút nào khi dữ liệu là mô phỏng.

**Bộ đếm khóa đăng nhập nằm trong tiến trình.** Sai mật khẩu quá 5 lần thì tài khoản bị khóa 15 phút, nhưng bộ đếm đó nằm trong bộ nhớ của một tiến trình. Chạy hai bản sao sau load balancer thì có hai bộ đếm riêng, và kẻ tấn công rải đều các lần đoán qua cả hai sẽ được gấp đôi số lượt. Ở quy mô này, đó là đánh đổi đúng so với việc kéo thêm Redis vào; nhưng nếu dịch vụ chạy nhiều hơn một instance thì đây là thứ đầu tiên phải đưa ra ngoài. Xem [trang 10](10-xac-thuc.md).

**Chưa có refresh token, chưa có thu hồi token.** Access token sống 60 phút, và trong 60 phút đó không có cách nào rút nó lại trước hạn. Claim `jti` đã được ghi sẵn trong mỗi token để một danh sách thu hồi sau này có thể lấy làm khóa, nhưng danh sách đó chưa tồn tại.

**Chưa có tool hủy đăng ký.** Sinh viên bị vượt trần tín chỉ hiện phải liên hệ phòng đào tạo. Đây là tool tiếp theo đáng làm, và nó cũng cần transaction (trả chỗ về cho lớp, giảm `enrolled`).

**Cảnh báo học vụ mức 2 chưa sinh ra tự động.** Dữ liệu mô phỏng không có lịch sử điểm theo từng học kỳ liên tiếp, mà quy chế đòi phải xét kết quả của kỳ trước để phân biệt mức 1 với mức 2. Guardrail vẫn xử lý đúng mức 2 nếu dữ liệu có, chỉ là seed không sinh ra nó.

**Một câu hỏi RAG vẫn trượt.** "Giải tích 1 bao nhiêu tín chỉ?" lấy nhầm phần mở đầu của chương trình đào tạo. Nguyên nhân và cách sửa ở [trang 3](03-rag-pipeline.md).

**Retriever nạp một lần lúc khởi động.** Nếu nạp lại tài liệu bằng `scripts/ingest.py` trong lúc service đang chạy, service vẫn dùng bản cũ trong bộ nhớ cho tới khi khởi động lại. Với quy mô này thì chấp nhận được, nhưng nó là một cái bẫy đáng biết.

## Hướng phát triển tiếp

Xếp theo giá trị mang lại, không theo độ khó.

**1. Tool hủy đăng ký.** Hoàn thiện vòng đời của một lệnh đăng ký, và mang thêm một bài toán transaction nữa (trả chỗ về cho lớp mà không để sĩ số âm, đã có `CHECK (enrolled >= 0)` sẵn).

**2. Gộp `tim_lop_hoc_phan` vào `dang_ky_hoc_phan`.** Hiện model thường gọi hai tool liên tiếp để đăng ký, tốn một vòng lặp thừa. Cho phép `dang_ky_hoc_phan` nhận mã học phần thay vì mã lớp sẽ bớt được một vòng, tức là bớt một lời gọi Gemini mỗi lần đăng ký. Đây là tối ưu chi phí có ý nghĩa nhất, vì nó nhắm đúng vào chỗ tốn tiền.

**3. Gắn tiêu đề tài liệu vào đầu mỗi đoạn khi sinh embedding.** Sửa được câu hỏi RAG đang trượt, và nhiều khả năng nâng luôn Recall@1 lên trên 80%.

**4. Cache embedding của câu hỏi lặp lại.** Nhiều sinh viên hỏi cùng một câu về quy chế. Một LRU cache nhỏ sẽ bớt được một lời gọi embedding mỗi lần trúng cache.

**5. Xác thực thật.** Đọc `student_id` từ JWT thay vì từ body của request.

## Nếu làm lại từ đầu

Hai thứ sẽ làm khác:

**Viết bài test đồng thời sớm hơn.** Nó là thứ dạy được nhiều nhất về hệ thống, và nó lộ ra sự phân vai giữa `CHECK` và `FOR UPDATE` mà nếu chỉ ngồi nghĩ thì không bao giờ thấy được.

**Viết thông điệp từ chối cẩn thận hơn ngay từ đầu.** Bug thông điệp trùng lịch (nêu lịch của lớp này nhưng đặt cạnh tên lớp kia) chỉ lộ ra khi đọc câu trả lời của model, chứ không lộ ra qua test. Lời từ chối sẽ được model đọc lại cho người dùng nghe, nên nó **là một phần của giao diện**, không phải chỉ là một chuỗi log.

---

[← Trang trước: Kiểm thử](08-kiem-thu.md) | [Mục lục](../README.md#tài-liệu-chi-tiết)
