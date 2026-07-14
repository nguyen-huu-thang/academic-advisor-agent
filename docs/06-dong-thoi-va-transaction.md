[← Trang trước: Guardrail](05-guardrail.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Đo lường và chi phí →](07-do-luong-va-chi-phi.md)

---

# 6. Đồng thời và transaction

## Vấn đề

Guardrail đọc sĩ số lớp ở **ngoài mọi transaction**, từ trước khi model được hỏi phải làm gì. Đến lúc một lệnh xác nhận thực sự chạy, con số đó có thể đã cũ vài giây.

Trong đợt cao điểm đăng ký, nhiều sinh viên cùng lao vào chỗ ngồi cuối cùng của một lớp trong cùng một khoảnh khắc. Đây không phải tình huống hiếm gặp cần tưởng tượng ra: đây là **chuyện xảy ra hằng kỳ** ở mọi trường đại học.

Nếu hai người cùng đọc "44 trên 45" rồi cùng kết luận là còn chỗ, cả hai sẽ cùng ghi tên mình vào.

## Cách xử lý

`xac_nhan_dang_ky` chạy trong một transaction, gồm hai lớp bảo vệ:

### Lớp 1: giành phiếu bằng chính câu UPDATE

```sql
UPDATE pending_registrations
SET status = 'da_thuc_hien'
WHERE id = %s AND status = 'cho_xac_nhan' AND expires_at > now()
RETURNING student_id, class_section_id
```

Nếu câu lệnh này **không cập nhật được dòng nào**, nghĩa là phiếu đã bị dùng rồi hoặc đã hết hạn, và ta dừng lại.

Điều tinh tế ở đây: việc **kiểm tra** và việc **chiếm** phiếu là **cùng một câu lệnh**. Không có khoảng hở nào giữa "kiểm tra thấy phiếu còn dùng được" và "đánh dấu phiếu đã dùng" để một request khác chen vào.

Nhờ vậy, một lần xác nhận thứ hai trên cùng mã phiếu, dù đến từ một request HTTP bị gửi lại hay từ việc model gọi tool hai lần, sẽ **không cập nhật dòng nào và không ghi danh ai cả**.

### Lớp 2: khóa dòng sinh viên

```sql
SELECT student_id, academic_status FROM students
WHERE student_id = %s
FOR UPDATE
```

Cái khóa này bịt một lỗ hổng mà khóa lớp **không bao giờ chạm tới được**, và nó không hiển nhiên.

Khóa lớp bảo vệ sĩ số, vốn bị tranh chấp giữa **những sinh viên khác nhau**. Nhưng trần tín chỉ và trùng lịch lại là thuộc tính của tập các lớp của **một** sinh viên. Chúng bị tranh chấp khi chính sinh viên đó xác nhận **hai phiếu cùng lúc**: hai tab trình duyệt, một request bị gửi lại, hay một model gọi cả hai lệnh xác nhận trong cùng một câu trả lời.

```text
Luồng A: xác nhận phiếu lớp Toán (thứ 5, tiết 1-3)
Luồng B: xác nhận phiếu lớp Lý  (thứ 5, tiết 2-4)

Cả hai đọc cùng một danh sách lớp đã đăng ký: rỗng.
Cả hai thấy không trùng lịch.
Cả hai đi qua.
```

Sinh viên bị xếp vào hai lớp cùng một khung giờ. Hai lớp đều còn thừa chỗ, nên khóa lớp không có việc gì để làm - cuộc va chạm nằm hoàn toàn bên trong thời khóa biểu của một người.

Khóa dòng `students` khiến **mọi lệnh xác nhận của cùng một sinh viên phải xếp hàng sau nhau**, nên danh sách lớp đọc ở dưới là danh sách tại thời điểm này, không phải từ đầu lượt.

**Thứ tự khóa là cố định ở khắp nơi: sinh viên trước, lớp sau.** Hai transaction lấy hai khóa này theo hai thứ tự ngược nhau thì sớm muộn cũng sẽ chờ nhau mãi mãi.

### Lớp 3: khóa dòng lớp rồi kiểm tra lại toàn bộ luật

```sql
SELECT s.capacity, s.enrolled, ...
FROM class_sections s
WHERE s.id = %s
FOR UPDATE OF s
```

`FOR UPDATE` khóa dòng của lớp. Mọi lệnh xác nhận khác vào cùng lớp này từ giờ **phải xếp hàng** sau câu lệnh này.

Giờ đã giữ cả hai khóa, toàn bộ **sáu luật** được chạy lại trên dữ liệu đọc mới bên trong transaction:

```python
fresh = TurnContext(
    registration_open = _registration_open(conn, semester),   # đọc lại
    passed_courses    = _passed_courses(conn, student_id),    # đọc lại
    registered        = _registered(conn, student_id, semester),  # đọc lại, dưới khóa
    target            = target,                               # đọc lại, dưới khóa
    ...
)
decision = check_registration_rules(target, fresh)
if not decision.allowed:
    raise RegistrationRejected(decision.note)   # transaction rollback
```

Ba điều đáng nói.

**Chạy lại chính hàm thuần mà guardrail đã dùng**, không viết lại luật ở đây. Hai bản sao của sáu quy tắc rồi sẽ có lúc lệch nhau, và bản sao lệch một cách âm thầm sẽ đúng là bản đang canh lệnh ghi.

**Đọc lại trên chính `conn` của transaction**, không mở connection mới. Một sự thật đọc lại trên một connection **khác** thì được đọc bên ngoài transaction, sẽ không nhìn thấy các khóa mà transaction này đang giữ, và vì vậy sẽ đúng là cái sự thật cũ kỹ mà cái khóa sinh ra để tránh.

**`RegistrationRejected` được ném ra bên trong transaction**, nên PostgreSQL hủy bỏ toàn bộ, kể cả cái phiếu vừa bị đánh dấu là đã thực hiện ở lớp 1. Phiếu quay lại trạng thái chờ.

Đây là lần kiểm tra **có hiệu lực**. Lần kiểm tra ở guardrail chỉ tồn tại để báo sớm cho sinh viên.

### Lớp 4: ràng buộc của database

```sql
CHECK (enrolled >= 0 AND enrolled <= capacity)
```

Nằm dưới toàn bộ logic ứng dụng. Nếu một lần refactor nào đó sau này làm mất cái khóa, PostgreSQL vẫn không cho lớp chứa nhiều sinh viên hơn số chỗ nó có.

## Thí nghiệm: cái khóa thực sự làm gì

Đây là phần đáng giá nhất của cả dự án, vì nó là thứ **đo được chứ không phải suy đoán được**.

Câu hỏi: nếu bỏ `FOR UPDATE` đi thì chuyện gì xảy ra?

Câu trả lời hiển nhiên là "dữ liệu sẽ sai, lớp sẽ bị vượt sĩ số". **Câu trả lời hiển nhiên đó sai.**

### Cách làm

Chạy lại đúng kịch bản 20 luồng tranh một chỗ, trên một bản sao của `execute_registration` đã bỏ `FOR UPDATE`. Mỗi luồng mở connection riêng, và tất cả cùng chờ ở một `threading.Barrier` để đảm bảo chúng thực sự lao vào cùng lúc chứ không lác đác vào từng cái một.

### Kết quả

```
KHONG CO FOR UPDATE:
  so lan thanh cong        : 1     (dung ra phai la 1)
  bi database CHECK chan   : 19
  si so cuoi cung          : 1/1
  so dong trong enrollments: 1
```

**Dữ liệu vẫn đúng.** Sĩ số cuối cùng vẫn là 1 trên 1, vẫn đúng một dòng trong `enrollments`. Ràng buộc `CHECK` giữ được phòng tuyến.

Nhưng 19 luồng còn lại thất bại bằng **`CheckViolation` do PostgreSQL ném ra**, chứ không phải bằng `RegistrationRejected`.

### Vì sao điều đó quan trọng

Trong dịch vụ đang chạy, `CheckViolation` rơi vào `except Exception` ở [loop.py](../app/agent/loop.py), và biến thành:

> *"Tool gặp lỗi khi thực thi. Hãy bảo sinh viên thử lại sau."*

Tức là **19 sinh viên nhận một lỗi 500**, thay vì câu:

> *"Lớp vừa hết chỗ, em chọn lớp khác của cùng học phần nhé."*

### Kết luận

**Hai lớp phòng thủ làm hai việc khác nhau, và cần cả hai:**

| Lớp | Bảo vệ điều gì |
|---|---|
| `CHECK (enrolled <= capacity)` | **Tính đúng đắn của dữ liệu.** Lớp không bao giờ vượt sĩ số, dù code có sai |
| `SELECT ... FOR UPDATE` | **Chất lượng của câu trả lời.** Biến một lỗi vi phạm ràng buộc thành một lời từ chối có nghĩa |

Đây là một phân biệt mà nhiều người bỏ qua. Rất dễ nghĩ rằng "có `CHECK` rồi thì cần gì khóa", hoặc ngược lại "có khóa rồi thì `CHECK` là thừa". Cả hai suy nghĩ đều sai, và lý do chỉ lộ ra khi thực sự chạy thí nghiệm.

## Thí nghiệm thứ hai: khóa dòng sinh viên có thật sự cần không

Cùng một phương pháp, cho cái khóa còn lại. Bỏ `FOR UPDATE` trên dòng `students`, rồi cho **một** sinh viên xác nhận **hai lớp trùng lịch** cùng lúc. Hai lớp đều còn 50 chỗ trống, nên khóa lớp không có việc gì để làm.

```text
KHONG CO FOR UPDATE tren dong students:
  so lan thanh cong : 2     (dung ra phai la 1)
  bi tu choi        : 0
  so lop cua sinh vien trong hoc ky: 2 lop TRUNG LICH nhau
```

Lần này thì **không có lớp phòng thủ nào đỡ được**. Ràng buộc `CHECK` chỉ biết đếm sĩ số, nó không biết gì về thời khóa biểu. `UNIQUE (student_id, course_code, semester)` chỉ chặn đăng ký trùng **môn**, mà đây là hai môn khác nhau. Không có ràng buộc database nào diễn tả được "hai lớp của cùng một sinh viên không được chồng tiết".

Nên khác biệt với thí nghiệm thứ nhất là ở chỗ này:

| | Bỏ khóa thì mất gì |
|---|---|
| Khóa dòng **lớp** | Mất **chất lượng câu trả lời**. Dữ liệu vẫn đúng nhờ `CHECK` |
| Khóa dòng **sinh viên** | Mất **tính đúng đắn của dữ liệu**. Không có hàng rào nào ở dưới |

Bài học chung của cả hai thí nghiệm: **một ràng buộc database chỉ bảo vệ được thứ nó diễn tả được.** `CHECK (enrolled <= capacity)` diễn tả được sĩ số, nên nó là hàng rào cuối cho sĩ số. Không có gì diễn tả được trùng lịch, nên ở đó cái khóa **là** hàng rào cuối, không phải hàng rào phụ.

## Bài test

`tests/test_registration_concurrency.py`, đánh dấu là integration test vì cần PostgreSQL thật. **Một cái khóa không bao giờ bị tranh chấp thì không chứng minh được điều gì.**

Bốn test:

**1. Chỉ một sinh viên giành được chỗ cuối.** 20 luồng, mỗi luồng một connection riêng, cùng chờ ở một barrier. Kiểm tra:

```python
assert len(results) == CONTENDERS          # 20 luồng đều quay về có kiểm soát
assert results.count("thanh_cong") == 1
assert results.count("bi_tu_choi") == 19
assert section["enrolled"] == 1
assert enrolled_rows == 1                  # bộ đếm khớp với số dòng thật
```

Phép khẳng định đầu tiên là phép quan trọng nhất, và nó không hiển nhiên. Một luồng chết vì `CheckViolation` thô sẽ **không kịp ghi lại kết quả nào**, nên một danh sách bị thiếu tự nó đã là dấu hiệu cho thấy cái khóa không làm đúng việc. Nói cách khác: bài test này **fail nếu bỏ `FOR UPDATE`**, đúng như một bài test đồng thời cần phải thế.

**2. Một sinh viên không thể xác nhận hai lớp trùng lịch cùng lúc.** Hai luồng, cùng một sinh viên, hai lớp chồng tiết nhau, cả hai lớp đều còn thừa chỗ. Khẳng định đúng một lớp lọt, và kiểm chứng lại bằng cách đếm thẳng trong `enrollments` chứ không tin vào báo cáo của chính các luồng:

```python
assert results.count("thanh_cong") == 1
assert enrolments["n"] == 1     # đọc từ database
```

Bài test này **fail nếu bỏ `FOR UPDATE` trên dòng `students`**: cả hai luồng đều thành công, và sinh viên có hai lớp trùng lịch trong học kỳ. Đã chạy thí nghiệm để xác nhận đúng như vậy.

**3. Xác nhận hai lần chỉ ghi danh một lần.** Một request HTTP bị gửi lại, hay một model gọi tool hai lần, không được phép ghi danh sinh viên hai lần.

**4. Database từ chối làm quá tải lớp ngay cả khi code sai.** Cố tình chạy `UPDATE class_sections SET enrolled = capacity + 1` và khẳng định PostgreSQL ném `CheckViolation`. Đây là bài test cho lớp phòng thủ cuối cùng.

## Vì sao khóa dòng lớp chứ không khóa bảng

Khóa cả bảng `class_sections` cũng giải quyết được vấn đề, và đơn giản hơn. Nhưng khi đó **mọi lệnh đăng ký vào mọi lớp** đều phải xếp hàng sau nhau, kể cả hai sinh viên đăng ký hai lớp hoàn toàn không liên quan.

Khóa dòng thì chỉ những người tranh nhau **cùng một lớp** mới phải chờ nhau. Trong đợt cao điểm đăng ký, đó là khác biệt giữa một hệ thống dùng được và một hệ thống tắc nghẽn.

Đây là đánh đổi kinh điển giữa mức độ chi tiết của khóa và thông lượng, và ở đây câu trả lời rõ ràng: khóa càng hẹp càng tốt, miễn là vẫn đủ để bảo vệ bất biến cần bảo vệ.

---

[← Trang trước: Guardrail](05-guardrail.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Đo lường và chi phí →](07-do-luong-va-chi-phi.md)
