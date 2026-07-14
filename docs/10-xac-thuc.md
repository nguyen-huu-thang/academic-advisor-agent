[Trang trước: 9. Quyết định thiết kế](09-quyet-dinh-thiet-ke.md) | [Về README](../README.md)

---

# 10. Xác thực

Trang này nói về một lỗ hổng có thật trong dự án, vì sao nó tồn tại, và nó đã được vá thế nào.

## Lỗ hổng

Toàn bộ lập luận về bảo mật của dự án này đứng trên đúng một câu: *trợ lý chỉ phục vụ một sinh viên đã xác thực*.

Câu đó là lý do các tool đọc hồ sơ không nhận tham số mã sinh viên. Đó là một quyết định mình vẫn cho là đúng: cách chắc chắn nhất để model không đọc được bảng điểm của người khác là không cho nó một ô trống nào để điền mã người khác vào.

Nhưng mã sinh viên khi đó lại nằm trong body của request:

```json
POST /chat
{ "session_id": "bat-ky", "student_id": "22021003", "message": "cho xem bảng điểm của tôi" }
```

Không có tầng xác thực nào cả. Cái ô trống bị bịt ở tầng tool thì lại mở toang ở tầng ngoài, cách đó đúng một lớp. Bất kỳ ai gọi được API đều đọc được bảng điểm, GPA và tiến độ học tập của bất kỳ sinh viên nào, chỉ bằng cách gõ mã của họ vào.

Tài liệu cũ có ghi nhận điều này, dưới dạng *"service giả định `student_id` đến từ một tầng đã xác thực sẵn"*. Nhưng một giả định không được kiểm tra thì không phải là một giả định, nó là một lỗ hổng có kèm lời xin lỗi. Không có tầng nào ở trước service này cả, và câu đó chỉ nói lên rằng mình đã biết chỗ này thủng.

## Nguyên tắc để vá

Nguyên tắc là **áp dụng lại đúng nước đi đã dùng cho schema của tool, ra thêm một lớp nữa**.

Cách vá dễ nghĩ nhất là giữ `student_id` trong body, rồi so nó với token và từ chối nếu lệch. Cách đó chạy được, nhưng nó để lại một trường mà một ngày nào đó có người quên kiểm tra. Một trường không tồn tại thì không thể quên kiểm tra nó.

Nên `student_id` bị **xóa hẳn** khỏi `ChatRequest`. Mã sinh viên đến từ claim `sub` của một JWT đã ký, và không đến từ đâu khác. Một `student_id` gửi kèm trong body hôm nay không bị từ chối, và cũng không được đối chiếu - nó đơn giản là **không được đọc**, vì nó không còn là một trường của model đó nữa.

```python
class ChatRequest(BaseModel):
    session_id: str
    message: str
    # Khong con student_id o day. Do la chu y.

def chat(payload: ChatRequest, student_id: str = Depends(get_current_student)):
    ...
```

`get_current_student` là con đường duy nhất một mã sinh viên đi vào được dịch vụ.

## Kiểm chứng: bắn lại đúng đòn tấn công cũ

Đăng nhập bằng tài khoản của An (22021001), rồi gõ mã của Cường (22021003) vào body, đúng như trước kia:

```bash
curl -X POST localhost:8000/chat \
  -H "Authorization: Bearer <token cua An>" \
  -d '{"session_id":"attack1","student_id":"22021003","message":"Toi ten gi, GPA bao nhieu?"}'
```

Trợ lý trả lời:

```text
Họ và tên: Nguyễn Văn An
GPA tích lũy: 2.84
Tình trạng học vụ: Bình thường
```

Cường có GPA 3,73. Trợ lý trả lời với tư cách An, đúng sinh viên mà token chứng minh được.

Và kiểm chứng lại bằng database, không hỏi lại trợ lý:

```sql
SELECT student_id, tool_name FROM tool_audit_log WHERE session_id = 'attack1';
-- 22021001  tra_cuu_tien_do_hoc_tap
```

Nhật ký kiểm toán ghi 22021001, không phải 22021003.

## Bốn quyết định trong tầng này

### 1. Băm mật khẩu bằng scrypt, không phải SHA-256

Một hàm băm nhanh là **điểm yếu** trong bảng mật khẩu, không phải ưu điểm. SHA-256 được thiết kế để chạy thật nhanh, và đó đúng là tính chất sai ở đây: nó cho phép kẻ đánh cắp được bảng thử hàng tỷ lần đoán mỗi giây.

scrypt thì cố tình chậm và ngốn bộ nhớ. Với tham số đang dùng (N = 2^14, r = 8, p = 1), mỗi lần băm tốn 16 MB RAM, nên mỗi lần đoán đều bắt kẻ tấn công trả giá bằng RAM thật và thời gian thật. Nó có sẵn trong `hashlib` của thư viện chuẩn, nên không phải thêm phụ thuộc nào.

Mỗi bản băm mang một salt ngẫu nhiên riêng, nên hai sinh viên lỡ đặt trùng mật khẩu vẫn cho ra hai bản băm khác nhau. Không có salt thì một mật khẩu bị bẻ là mở được mọi tài khoản dùng chung mật khẩu đó.

So sánh bằng `hmac.compare_digest`, tức là trong thời gian hằng định. Phép `==` thông thường dừng lại ngay ở byte đầu tiên khác nhau, và thời gian nó dừng lại lộ ra có bao nhiêu byte đầu đã đúng - qua nhiều lần thử, đó là đủ để dựng lại bản băm từng byte một.

### 2. Ghim thuật toán JWT, không đọc nó từ token

Hai lỗ hổng kinh điển nằm gọn trong một dòng:

```python
jwt.decode(token, secret, algorithms=["HS256"], ...)
```

Một token có header ghi `alg: none` thì không mang chữ ký nào cả, và một bộ giải mã nào đọc thuật toán từ chính token sẽ vui vẻ chấp nhận nó. Một token ghi `alg: RS256` thì "chữ ký" của nó sẽ bị kiểm tra bằng secret của ta như thể secret đó là một khóa công khai.

Tự mình chỉ định thuật toán, thay vì để token tự khai, chặn được cả hai: **token không còn được quyền quyết định nó sẽ bị kiểm tra ra sao.** Cả hai trường hợp đều có test.

`iss` và `aud` cũng được kiểm tra, vì một token được ký đúng chưa chắc là một token dành cho ta.

### 3. Sai mật khẩu và sai mã sinh viên phải trả lời giống hệt nhau

Cả hai đều trả về `401` với cùng một câu: *"Ma sinh vien hoac mat khau khong dung."*

Nếu phân biệt hai trường hợp - bằng một thông báo khác đi, **hay chỉ đơn giản bằng một câu trả lời nhanh hơn** - thì endpoint đăng nhập trở thành một cách để dò xem những mã sinh viên nào có thật.

Vế thứ hai mới là vế dễ quên. Khi mã sinh viên không tồn tại thì không có bản băm nào để kiểm tra, nên một hàm đăng nhập ngây thơ sẽ trả lời **ngay lập tức**, trong khi một lần đăng nhập của sinh viên có thật với mật khẩu sai lại phải chịu trọn 16 MB và mấy chục mili giây của scrypt. Chênh lệch đó đọc được từ bên ngoài. Vì vậy khi không tìm thấy sinh viên, service vẫn đem mật khẩu ra đối chiếu với một bản băm giả, để hai đường đi tốn thời gian như nhau.

### 4. Khóa tạm sau nhiều lần sai

scrypt làm mỗi lần đoán trở nên đắt, nhưng đắt không có nghĩa là bất khả thi, và mật khẩu sinh viên thì thường ngắn. Sai 5 lần thì khóa 15 phút, trả `429` kèm `Retry-After`.

Khóa theo từng mã sinh viên chứ không khóa theo IP, và điều đó **có cái giá của nó**: về lý thuyết, một người có thể cố tình nhập sai để khóa tài khoản của bạn cùng lớp. Đổi lại, nó chặn được đúng thứ nguy hiểm hơn - dò mật khẩu của một tài khoản cụ thể. Một hệ thống thật nên khóa theo cả hai chiều.

Bộ đếm nằm trong bộ nhớ của tiến trình. Nói thẳng ra: chạy hai bản sao sau load balancer thì có hai bộ đếm riêng, và kẻ tấn công rải đều các lần đoán qua cả hai sẽ được gấp đôi số lượt. Ở quy mô này thì đó là đánh đổi đúng so với việc kéo thêm Redis vào; nhưng nếu dịch vụ chạy nhiều hơn một instance, đây là thứ đầu tiên phải đưa ra ngoài.

## Refresh token: hai loại token, hai đánh đổi ngược nhau

**Access token** là JWT và **không được lưu ở đâu cả**. Nó chỉ được kiểm tra bằng chữ ký, nên phục vụ một request không tốn một vòng gọi database nào. Cái giá phải trả là **không rút lại được trước hạn**.

Nên nó chỉ sống **15 phút**. Đây là chỗ đáng nói: thời gian sống của access token **chính là** độ trễ của việc thu hồi. Một sinh viên bị thu hồi phiên vẫn dùng được tối đa 15 phút nữa. Muốn cửa sổ đó bằng 0 thì phải tra database ở mọi request, và khi đó JWT chẳng còn ý nghĩa gì. Đó là một cái giá, và nó đáng được gọi tên ra thay vì để ngầm.

**Refresh token** thì đánh đổi ngược lại. Nó sống 14 ngày, nên **bắt buộc phải thu hồi được**, mà một thứ chỉ thu hồi được khi ở đâu đó có một dòng ghi rằng nó còn hiệu lực. Nó lại chỉ được trình ra 15 phút một lần chứ không phải mỗi request, nên phép tra cứu đó không tốn gì đáng kể.

**Không trạng thái ở chỗ nóng, có trạng thái ở chỗ bắt buộc phải thu hồi được.**

### Nó đi trong cookie HttpOnly, không đi trong body

```text
Set-Cookie: refresh_token=...; HttpOnly; Secure; Max-Age=1209600;
            Path=/auth/session; SameSite=strict
```

| Thuộc tính | Nó chặn cái gì |
|---|---|
| `HttpOnly` | JavaScript không đọc được cookie. Một lỗ hổng **XSS** - cách phổ biến nhất để token bị đánh cắp trên trình duyệt - không chạm tới được chứng chỉ sống hai tuần này |
| `SameSite=strict` | Cookie được trình duyệt tự động đính kèm, và chính điều đó sinh ra bề mặt **CSRF**: một trang khác có thể khiến trình duyệt POST tới `/auth/session/refresh`. `strict` khiến cookie không được đính kèm vào request xuất phát từ nơi khác |
| `Secure` | Trình duyệt không bao giờ gửi nó qua HTTP trần |
| `Path=/auth/session` | Cookie **không** được gửi kèm `/chat`, và cũng không gửi kèm `/auth/login` |

Còn **access token thì trả trong body**, chính là để frontend giữ nó trong RAM và **không bao giờ bỏ vào `localStorage`**. Thứ gì nằm trong `localStorage` thì sống sót qua một lần tải lại trang, nghe thì tiện, cho tới khi nhớ ra rằng nó cũng sống sót đủ lâu để bất kỳ đoạn script nào được chèn vào cũng kịp mang đi.

Nói gọn: thứ mà script chạm tới được thì chỉ đáng giá 15 phút; thứ đáng giá hai tuần thì script không chạm tới được.

Về `Path`: chọn `/auth/session` chứ không phải `/auth`, vì `/auth/login` là endpoint **duy nhất xử lý mật khẩu** và nó không hề đọc cookie đó. Gửi cookie tới đó thì không được gì mà lại có thứ để mất. **Một chứng chỉ không bao giờ được gửi đi là một chứng chỉ không thể bị lộ bởi thứ xử lý cái request nó không được gửi tới.**

### Xoay vòng: token cũ chết ngay khi token mới ra đời

Mỗi lần refresh sinh ra một refresh token mới và giết token cũ. Nhờ vậy một token bị sao chép trên đường truyền chỉ còn giá trị **cho tới lần refresh tiếp theo của chủ thật sự**, thay vì còn giá trị suốt hai tuần.

Việc giành lấy token được làm bằng **chính câu `UPDATE` đang kiểm tra nó** - đúng cái mẹo mà phiếu đăng ký học phần đang dùng:

```sql
UPDATE refresh_tokens SET status = 'rotated'
WHERE token_hash = %s AND status = 'active' AND expires_at > now()
RETURNING student_id, family_id
```

Việc **kiểm tra** và việc **chiếm** là cùng một câu lệnh, nên hai lần refresh chạy đua với cùng một token không thể cùng thắng. Có test bắn 20 luồng để chứng minh.

### Câu hỏi khó: một token đã tiêu rồi lại xuất hiện thì sao?

Đây mới là phần đáng giá, và nó không phải là "cấp token mới".

Hoặc **kẻ trộm** đang tiêu bản sao sau lưng sinh viên, hoặc **chính sinh viên** đang gửi lại một request mà câu trả lời không bao giờ tới nơi. **Không có cách nào phân biệt hai trường hợp đó từ bên trong service.**

Nên service giả định trường hợp xấu hơn, và **thu hồi cả họ token** của lần đăng nhập đó:

```text
Kẻ trộm dùng lại token cũ  -> 401, cả họ bị thu hồi
Token thật của sinh viên   -> 401, chết theo
agent_refresh_reuse_total  -> 1
```

Token hợp lệ mà sinh viên đang cầm cũng chết. **Đó không phải là bug, đó là điểm mấu chốt.** Nếu là kẻ trộm, cả họ phải chết, không thì kẻ trộm giữ được phiên. Nếu là một lần gửi lại ngay tình, sinh viên bị đăng xuất và đăng nhập lại.

**Phiền toái thì khắc phục được; một phiên đang sống trong tay người khác thì không.**

### "Họ token" là gì

Mọi token sinh ra từ **một lần đăng nhập** đều chung một `family_id`: token cấp lúc đăng nhập, token thay thế nó, token thay thế token đó, và cứ thế.

Thu hồi cả họ chứ không chỉ token đang cầm là thứ làm cho việc đăng xuất **có ý nghĩa**. Nếu chỉ giết token được trình ra thì token cha của nó - đã bị xoay vòng, nhưng vẫn nằm trong bảng - sẽ sót lại, và một kẻ trộm đang giữ token cha đó vẫn cứ thế mà refresh tiếp.

Ngược lại, thu hồi phải **dừng lại ở ranh giới của họ**: mỗi lần đăng nhập mở một họ riêng, nên đăng nhập trên điện thoại không bị đăng xuất theo máy tính.

### Vì sao các dòng `rotated` được giữ lại chứ không xóa

Vì chính chúng là **bằng chứng duy nhất** cho phép nhận ra một token bị dùng lại là một lần dùng lại, thay vì là một token chưa ai từng thấy. Xóa chúng đi là vứt bỏ luôn khả năng phát hiện tái sử dụng.

Chúng chỉ bị xóa khi **đã hết hạn**, vì lúc đó một lần dùng lại cũng sẽ bị từ chối vì hết hạn rồi. Không có bước dọn này thì bảng phình thêm một dòng sau mỗi lần refresh, mãi mãi: một sinh viên cứ 15 phút refresh một lần trong một năm là 35 nghìn dòng không ai đọc tới.

### Vì sao băm refresh token bằng SHA-256 chứ không phải scrypt

Câu này nghe như mâu thuẫn với việc mật khẩu dùng scrypt, nhưng không.

**Mật khẩu thì ngắn và đoán được**, nên phải làm cho mỗi lần đoán trở nên đắt đỏ. **Refresh token là 256 bit ngẫu nhiên**: không ai đoán ra được nó cả, nên một hàm băm chậm chẳng mua được gì mà còn đặt một khoảng chờ lên mỗi lần refresh.

Bản băm ở đây tồn tại vì một mục đích hẹp hơn nhiều: nếu **bảng** bị đánh cắp, các dòng trong đó không thể đem trình cho service như một token. Làm cho một cái **bảng** bị cắp trở nên vô dụng, và làm cho một cái **token** bị cắp trở nên khó đoán, là hai việc khác nhau - và ở đây chỉ cần việc thứ nhất.

Cũng không có salt, và đó cũng là cố ý: bản băm **chính là khóa tra cứu**, mà một bản băm có salt thì không tra cứu được.

### Giới hạn đã biết, nói thẳng

Phát hiện tái sử dụng ở đây là **nghiêm ngặt, không có cửa sổ ân hạn**. Một client gửi lại lệnh refresh sau khi mất câu trả lời sẽ bị đăng xuất. Nhiều hệ thống thật cho một khoảng ân hạn vài giây, trong đó token vừa xoay vòng được trình lại thì trả về đúng token kế nhiệm cũ thay vì báo động.

Chọn nghiêm ngặt vì cán cân lệch hẳn về một phía: bị đăng xuất là phiền toái khắc phục được, còn để lọt một token bị dùng lại thì không.

## Endpoint vận hành dùng một khóa khác

`/metrics` và `/stats` nằm sau `METRICS_TOKEN`, và **token của sinh viên không mở được chúng**. Có test riêng khẳng định đúng điều đó, vì "nằm sau một lớp xác thực nào đó" và "nằm sau đúng lớp xác thực cần thiết" là hai chuyện khác nhau.

Lý do: hai endpoint này báo cáo số token đã tiêu, số tiền USD, và `agent_tool_denied_total` - số lần guardrail phải ra tay. Con số cuối cùng nói cho người ngoài biết chính xác khi nào đòn tấn công của họ chạm tới guardrail, vốn là thứ cuối cùng nên đưa cho họ.

`/health` thì vẫn công khai, vì load balancer phải gọi được nó mà không cầm theo bí mật nào.

## Không có giá trị mặc định nào là an toàn

`JWT_SECRET` và `METRICS_TOKEN` đều là bắt buộc, tối thiểu 32 ký tự, và **service từ chối khởi động** nếu thiếu hoặc quá ngắn.

Một khóa ký ngắn đến mức dò vét cạn ngoại tuyến được thì cũng như không có khóa ký: ai lấy lại được nó là cấp được token cho bất kỳ sinh viên nào. Ở đây không có giá trị mặc định nào là an toàn, nên dịch vụ không lấy đại một giá trị - nó chết ngay lúc khởi động, ở nơi lập trình viên nhìn thấy, thay vì chạy tiếp và mở cửa cho tất cả mọi người.

## Kiểm thử

**30 test không đụng tới database và không đụng tới mạng.** Mật khẩu, JWT và bộ đếm khóa đăng nhập đều là hàm thuần của đầu vào, còn đồng hồ thì được truyền vào chứ không đọc tại chỗ - chính vì vậy mà kiểm tra được một token hết hạn mà không phải ngồi đợi một tiếng đồng hồ.

**Thêm 9 test cho refresh token thì buộc phải có PostgreSQL**, và đó không phải chuyện cài đặt. Thu hồi được nghĩa là phải có trạng thái ở đâu đó, mà trạng thái lại đúng là thứ một hàm thuần không thể có. Ranh giới giữa "thuần" và "phải có database" nằm đúng ở chỗ này, và nó nằm ở đó vì một lý do nói ra được.

Những test đáng nói:

| Test | Điều nó khẳng định |
|---|---|
| `test_tampering_with_the_student_id_breaks_the_signature` | Đổi `sub` sang mã người khác thì chữ ký vỡ |
| `test_unsigned_token_is_refused` | `alg: none` bị từ chối |
| `test_token_meant_for_another_service_is_refused` | Ký đúng chưa chắc là dành cho ta |
| `test_token_without_an_expiry_is_refused` | Thiếu `exp` tự nó đã là lỗi, vì token không hạn thì sống vĩnh viễn |
| `test_empty_stored_hash_never_authenticates` | Một lần đổi lược đồ làm dở dang phải **đóng lại**, không phải mở ra |
| `test_throttle_locks_one_student_without_locking_another` | Khóa một người không được khóa lây người khác |
| `test_the_body_cannot_name_a_student` | `student_id` không còn là một trường của `ChatRequest`. Nếu sau này có ai thêm nó trở lại, test này đổ |
| `test_reusing_a_spent_token_revokes_the_entire_family` | Kẻ trộm dùng lại token đã tiêu thì **cả họ chết**, kể cả token thật của sinh viên |
| `test_each_login_starts_its_own_family` | Thu hồi phải dừng ở ranh giới của họ, không thì một tab hỏng đăng xuất hết mọi thiết bị |
| `test_logout_revokes_the_family_not_just_the_token_in_hand` | Giết mỗi token đang cầm thì token cha vẫn sống, và kẻ trộm giữ token cha vẫn refresh tiếp được |
| `test_two_refreshes_racing_with_one_token_cannot_both_win` | 20 luồng cùng trình một token thì đúng một luồng được phục vụ |
| `test_the_token_itself_is_never_written_to_the_database` | Bảng bị lộ thì các dòng trong đó vẫn không đem trình ra được như một token |

## Điều rút ra

Lỗ hổng này không nằm ở chỗ mình không biết về xác thực. Nó nằm ở chỗ mình đã bịt rất kỹ một ô trống ở tầng tool, rồi để nguyên đúng cái ô trống đó ở tầng ngay bên ngoài, và viết vào tài liệu một câu giả định để trấn an chính mình.

**Một biện pháp phòng thủ chỉ có giá trị tới đúng cái lớp mà nó được đặt vào.** Ranh giới đáng lo nhất luôn là cái ranh giới ta đã cho rằng có ai đó khác canh giùm.

---

[Trang trước: 9. Quyết định thiết kế](09-quyet-dinh-thiet-ke.md) | [Về README](../README.md)
