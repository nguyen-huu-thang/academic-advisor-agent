"""The grading rules of the university, in one place.

Cac quy tac ve diem cua nha truong, gom lai mot cho.

These rules also appear in data/documents/quy-che-dao-tao.md, which is what the assistant
quotes to the student. Keeping them in a single module means the number the assistant reads
out of the regulation and the number it computes with cannot drift apart: if the pass mark
were 4.0 in the document but 5.0 in the code, the assistant would confidently quote one rule
and then act on another.
Cac quy tac nay cung xuat hien trong data/documents/quy-che-dao-tao.md, la tai lieu ma tro ly
trich dan cho sinh vien. Giu chung o mot module duy nhat de con so tro ly doc tu quy che va
con so tro ly dung de tinh toan khong the lech nhau: neu diem dat la 4.0 trong tai lieu nhung
lai la 5.0 trong code, tro ly se doc mot dang quy tac roi hanh dong theo mot dang khac.
"""

from decimal import Decimal

# A course is passed from 4.0 on the 10-point scale, which is grade D.
# Mot hoc phan duoc coi la dat tu 4.0 tren thang diem 10, tuc la loai D.
PASS_MARK = Decimal("4.0")

# Lower bound on the 10-point scale, the letter grade, and the value on the 4-point scale.
# Ordered from highest to lowest so the first match wins.
# Nguong duoi tren thang diem 10, diem chu, va gia tri tren thang diem 4.
# Sap xep tu cao xuong thap de moc dau tien khop la moc dung.
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

    Diem chu tuong ung voi mot diem so tren thang diem 10.
    """
    for threshold, letter, _ in GRADE_SCALE:
        if score >= threshold:
            return letter
    return "F"


def grade_point(score: Decimal) -> Decimal:
    """The 4-point value for a mark on the 10-point scale.

    Gia tri tren thang diem 4 tuong ung voi mot diem so tren thang diem 10.
    """
    for threshold, _, point in GRADE_SCALE:
        if score >= threshold:
            return point
    return Decimal("0.0")


def is_passed(score: Decimal) -> bool:
    """Whether a mark counts as passing the course.

    Diem so nay co duoc tinh la dat hoc phan hay khong.
    """
    return score >= PASS_MARK


def compute_gpa(entries: list[tuple[Decimal, int]]) -> Decimal:
    """Weighted mean on the 4-point scale over (score, credits) pairs.

    Trung binh co trong so tren thang diem 4, tren cac cap (diem, so tin chi).

    Failed courses are included in the denominator with 0 points, exactly as the regulation
    says. Dropping them would quietly inflate the GPA of a student who has failed a lot,
    which is the opposite of what an academic warning is meant to catch.
    Hoc phan truot van duoc tinh vao mau so voi 0 diem, dung nhu quy che quy dinh. Neu bo
    chung ra, GPA cua mot sinh vien truot nhieu se bi thoi phong len, trai nguoc han voi muc
    dich cua viec canh bao hoc vu.
    """
    total_credits = sum(credits for _, credits in entries)
    if total_credits == 0:
        return Decimal("0.00")

    total_points = sum(grade_point(score) * credits for score, credits in entries)
    return (total_points / total_credits).quantize(Decimal("0.01"))


def earned_credits(entries: list[tuple[Decimal, int]]) -> int:
    """Credits from passed courses only. Failed courses earn nothing.

    So tin chi tich luy, chi tinh cac hoc phan da dat. Hoc phan truot khong duoc tinh.
    """
    return sum(credits for score, credits in entries if is_passed(score))
