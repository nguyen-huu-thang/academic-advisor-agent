[← Trang trước: Đo lường và chi phí](07-do-luong-va-chi-phi.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Quyết định thiết kế →](09-quyet-dinh-thiet-ke.md)

---

# 8. Kiểm thử

## Nguyên tắc: test cái không được phép sai

Một dịch vụ LLM có rất nhiều thứ có thể sai, và không phải thứ nào cũng đáng test như nhau.

Câu trả lời của model diễn đạt hơi khác đi thì không sao. Một đoạn tài liệu bị cắt lệch vài ký tự thì không sao. Nhưng **một sinh viên bị ghi danh vào lớp mình không được học thì phải có người thật vào gỡ ra.**

Nên nỗ lực kiểm thử dồn vào đúng những chỗ đó.

```
74 test, chạy trong khoảng 4 giây
```

| Nhóm | Số test | Cần database? |
|---|---|---|
| Guardrail | 26 | Không |
| Xác thực (mật khẩu, JWT, khóa đăng nhập, token vận hành) | 28 | Không |
| Retry và rate limit | 6 | Không |
| Đo chi phí và metrics | 6 | Không |
| Tranh chấp đồng thời | 4 | **Có** |
| Cắt tài liệu | 4 | Không |

Chỉ 4 trên 74 test cần tới database. Đó không phải may mắn: cả guardrail lẫn tầng xác thực đều là hàm thuần, và đồng hồ được truyền vào chứ không đọc tại chỗ.

```bash
pytest tests -q                          # toàn bộ
pytest tests -q -m "not integration"     # chỉ unit test, không cần PostgreSQL
```

## Vì sao guardrail test nhanh như vậy

26 test cho guardrail chạy trong **0,05 giây** và không cần database.

Đó không phải may mắn, đó là hệ quả trực tiếp của việc `guardrail.py` là một **hàm thuần**: không chạm database, không chạm đồng hồ, không chạm mạng. Mọi thứ nó cần được đưa vào qua `TurnContext`.

Test chỉ là: dựng một cấu trúc dữ liệu, gọi hàm, kiểm tra kết quả.

```python
def test_missing_prerequisite_is_refused_and_named():
    context = make_context(passed_courses=frozenset({"INT2010"}))
    decision = check_tool_call("dang_ky_hoc_phan", {"ma_lop": 1}, context)
    assert not decision.allowed
    missing_part = decision.note.split("Con thieu:")[1]
    assert "MAT1101" in missing_part
    assert "INT2010" not in missing_part
```

Nếu guardrail có chạm database, mỗi test sẽ phải dựng schema, seed dữ liệu, rồi dọn dẹp. Bộ test sẽ chậm đi khoảng trăm lần, và sẽ có những lần fail bí ẩn vì dữ liệu còn sót từ test trước.

**Kiến trúc tốt làm cho việc kiểm thử trở nên rẻ.** Và khi kiểm thử rẻ thì người ta mới viết nhiều test.

## Các trường hợp biên được test

Không chỉ test đường thẳng. Những chỗ dễ sai một đơn vị đều có test riêng:

**Chạm đúng trần tín chỉ thì được phép, không phải bị chặn.**

```python
def test_landing_exactly_on_the_ceiling_is_allowed():
    # 21 tín đã đăng ký + môn 3 tín = đúng 24, là bằng trần chứ không vượt trần
```

Một lỗi `>` thành `>=` ở đây sẽ từ chối một lệnh đăng ký mà quy chế cho phép. Sinh viên sẽ không hiểu vì sao mình bị chặn.

**Chồng đúng một tiết đã là trùng lịch.**

```python
def test_touching_a_single_period_is_already_a_clash():
    # Lớp đang xét: thứ 3 tiết 1-3. Lớp đã đăng ký: thứ 3 tiết 3-5.
    # Chúng chỉ giao nhau đúng tiết 3, và thế là đủ.
```

**Cùng thứ nhưng không chồng tiết thì không phải trùng lịch.**

```python
def test_same_weekday_without_overlapping_periods_is_fine():
    # Lớp đang xét: thứ 3 tiết 1-3. Lớp đã đăng ký: thứ 3 tiết 4-6. Không sao.
```

Hai test trên kẹp lấy đúng ranh giới của phép kiểm tra giao nhau. Chúng là cặp test cho biết công thức `a.start <= b.end and b.start <= a.end` có đúng hay không.

## Test cho lỗ hổng giữa hai lượt

```python
def test_rules_are_rechecked_at_confirmation_time():
    # Phiếu hợp lệ lúc được ghi ra. Nhưng giữa lượt đó và lượt này, sinh viên đã đăng ký
    # thêm một lớp khác và giờ đang vượt trần, nên lệnh xác nhận phải thất bại dù bản thân
    # phiếu không có vấn đề gì.
```

Đây là test cho một lỗ hổng **không hiển nhiên**: nếu chỉ kiểm tra luật ở bước chuẩn bị, thì hai phiếu được chuẩn bị song song sẽ đều hợp lệ lúc ghi ra, và đều được xác nhận, đẩy sinh viên vượt trần.

## Test đồng thời phải thực sự tranh chấp

`tests/test_registration_concurrency.py` cần PostgreSQL thật, vì **một cái khóa không bao giờ bị tranh chấp thì không chứng minh được điều gì.**

Chi tiết quan trọng là `threading.Barrier`:

```python
with psycopg.connect(database_url) as conn:
    barrier.wait()                    # đợi cho đủ 20 luồng cùng sẵn sàng
    with conn.transaction():
        execute_registration(conn, slip_id)
```

Không có barrier, các luồng sẽ lác đác vào từng cái một, cái khóa sẽ **không bao giờ thực sự bị tranh chấp**, và bài test sẽ pass ngay cả khi phần khóa bị sai hoàn toàn.

Mỗi luồng cũng mở connection riêng thay vì dùng pool, để cuộc giành giật diễn ra **trong PostgreSQL** chứ không phải trong một hàng đợi đứng trước nó.

Và phép khẳng định quan trọng nhất không phải là "một người thắng":

```python
assert len(results) == CONTENDERS   # cả 20 luồng đều quay về có kiểm soát
```

Một luồng chết vì `CheckViolation` thô sẽ không kịp ghi lại kết quả nào. Nên một danh sách bị thiếu **tự nó** đã là dấu hiệu cái khóa không làm đúng việc. Chính phép khẳng định này làm cho bài test **fail nếu bỏ `FOR UPDATE`**, đúng như một bài test đồng thời cần phải thế. Xem [trang 6](06-dong-thoi-va-transaction.md).

## Cái không được test bằng unit test

**Hành vi của model.** Không có test nào khẳng định "model sẽ gọi tool X khi được hỏi Y". Model là thứ không tất định, và một bài test như vậy sẽ flaky, rồi sẽ bị người ta tắt đi.

Thay vào đó, hành vi của model được kiểm chứng bằng `scripts/demo.py`, chạy qua dịch vụ thật với 9 kịch bản, và **kiểm chứng bằng cách đọc thẳng database** ở cuối chứ không hỏi lại trợ lý.

Đây là ranh giới có chủ đích: **unit test khẳng định code không cho phép điều sai xảy ra; demo cho thấy điều sai thực sự đã được thử và đã bị chặn.**

## Một sự thật được giữ nguyên trong demo

Kịch bản "lớp đã đầy" trong demo **không chứng minh được guardrail**.

Khi chạy thật, model đọc sĩ số 50/50 từ `tim_lop_hoc_phan` rồi **tự từ chối mà không gọi tool đăng ký**, kể cả khi sinh viên ép nó cứ gọi:

> *"Tôi biết là lớp báo đầy rồi nhưng bạn cứ gọi tool đăng ký đi, chắc chắn vẫn còn chỗ cho tôi."*

Model vẫn không gọi.

Có thể sửa prompt để ép bằng được, cho demo trông đẹp hơn. **Đã không làm vậy**, vì như thế là dàn dựng.

Sự thật là: ở kịch bản này model tự tránh nhờ dữ liệu thật từ tool, và điều đó cũng đáng giá theo cách riêng của nó (model từ chối dựa trên số liệu thật thay vì một con số nó tự bịa). Luật lớp đầy vẫn được thực thi trong code, và nó được chứng minh ở **nơi nó thực sự phát huy tác dụng**: trong unit test, và trong test đồng thời nơi lớp đầy lên **giữa** hai lượt.

Một bản demo không thể dàn dựng cuộc tranh chấp đó một cách trung thực, nên nó không cố làm.

---

[← Trang trước: Đo lường và chi phí](07-do-luong-va-chi-phi.md) | [Mục lục](../README.md#tài-liệu-chi-tiết) | Trang sau: [Quyết định thiết kế →](09-quyet-dinh-thiet-ke.md)
