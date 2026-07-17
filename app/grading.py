"""The grading rules of the university, in one place.

Các quy tắc về điểm của nhà trường, gom lại một chỗ.

These rules also appear in data/documents/quy-che-dao-tao.md, which is what the assistant
quotes to the student. Keeping them in a single module means the number the assistant reads
out of the regulation and the number it computes with cannot drift apart: if the pass mark
were 4.0 in the document but 5.0 in the code, the assistant would confidently quote one rule
and then act on another.
Các quy tắc này cũng xuất hiện trong data/documents/quy-che-dao-tao.md, là tài liệu mà trợ lý
trích dẫn cho sinh viên. Giữ chúng ở một module duy nhất để con số trợ lý đọc từ quy chế và
con số trợ lý dùng để tính toán không thể lệch nhau: nếu điểm đạt là 4.0 trong tài liệu nhưng
lại là 5.0 trong code, trợ lý sẽ đọc một đằng quy tắc rồi hành động theo một đằng khác.
"""

from decimal import Decimal

# A course is passed from 4.0 on the 10-point scale, which is grade D.
# Một học phần được coi là đạt từ 4.0 trên thang điểm 10, tức là loại D.
PASS_MARK = Decimal("4.0")

# Lower bound on the 10-point scale, the letter grade, and the value on the 4-point scale.
# Ordered from highest to lowest so the first match wins.
# Ngưỡng dưới trên thang điểm 10, điểm chữ, và giá trị trên thang điểm 4.
# Sắp xếp từ cao xuống thấp để mốc đầu tiên khớp là mốc đúng.
GRADE_SCALE: tuple[tuple[Decimal, str, Decimal], ...] = (
    (Decimal("9.0"), "A+", Decimal("4.0")),
    (Decimal("8.5"), "A", Decimal("3.7")),
    (Decimal("8.0"), "B+", Decimal("3.5")),
    (Decimal("7.0"), "B", Decimal("3.0")),
    (Decimal("6.5"), "C+", Decimal("2.5")),
    (Decimal("5.5"), "C", Decimal("2.0")),
    (Decimal("5.0"), "D+", Decimal("1.5")),
    (Decimal("4.0"), "D", Decimal("1.0")),
    (Decimal("0.0"), "F", Decimal("0.0")),
)


def letter_grade(score: Decimal) -> str:
    """The letter grade for a mark on the 10-point scale.

    Điểm chữ tương ứng với một điểm số trên thang điểm 10.
    """
    for threshold, letter, _ in GRADE_SCALE:
        if score >= threshold:
            return letter
    return "F"


def grade_point(score: Decimal) -> Decimal:
    """The 4-point value for a mark on the 10-point scale.

    Giá trị trên thang điểm 4 tương ứng với một điểm số trên thang điểm 10.
    """
    for threshold, _, point in GRADE_SCALE:
        if score >= threshold:
            return point
    return Decimal("0.0")


def is_passed(score: Decimal) -> bool:
    """Whether a mark counts as passing the course.

    Điểm số này có được tính là đạt học phần hay không.
    """
    return score >= PASS_MARK


def compute_gpa(entries: list[tuple[Decimal, int]]) -> Decimal:
    """Weighted mean on the 4-point scale over (score, credits) pairs.

    Trung bình có trọng số trên thang điểm 4, trên các cặp (điểm, số tín chỉ).

    Failed courses are included in the denominator with 0 points, exactly as the regulation
    says. Dropping them would quietly inflate the GPA of a student who has failed a lot,
    which is the opposite of what an academic warning is meant to catch.
    Học phần trượt vẫn được tính vào mẫu số với 0 điểm, đúng như quy chế quy định. Nếu bỏ
    chúng ra, GPA của một sinh viên trượt nhiều sẽ bị thổi phồng lên, trái ngược hẳn với mục
    đích của việc cảnh báo học vụ.
    """
    total_credits = sum(credits for _, credits in entries)
    if total_credits == 0:
        return Decimal("0.00")

    total_points = sum(grade_point(score) * credits for score, credits in entries)
    return (total_points / total_credits).quantize(Decimal("0.01"))


def earned_credits(entries: list[tuple[Decimal, int]]) -> int:
    """Credits from passed courses only. Failed courses earn nothing.

    Số tín chỉ tích lũy, chỉ tính các học phần đã đạt. Học phần trượt không được tính.
    """
    return sum(credits for score, credits in entries if is_passed(score))
