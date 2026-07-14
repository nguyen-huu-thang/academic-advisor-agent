"""Measure how often the retriever actually finds the right passage.

Do xem bo tim kiem thuc su lay dung doan tai lieu bao nhieu phan tram.

"The service has RAG" says nothing about whether the retrieval works. A pipeline that returns
the wrong passage still produces a fluent, confident, wrong answer - and the citation at the
bottom makes it look more trustworthy, not less. So the retriever is scored against a set of
questions whose correct passage is known in advance.
Cau "dich vu co RAG" khong noi len duoc gi ve chuyen tim kiem co chay dung hay khong. Mot pipeline
lay ve sai doan van thi van sinh ra mot cau tra loi tron tru, tu tin, va sai - ma dong trich nguon
o cuoi con lam no trong dang tin hon chu khong phai kem tin di. Vi vay bo tim kiem duoc cham diem
tren mot bo cau hoi da biet truoc doan van dung.

Two numbers are reported:

  Recall@k  - phan tram cau hoi co doan dung nam trong top k ket qua. Neu doan dung khong lot
              vao top k thi no khong bao gio den duoc tay model, va model chi con cach doan.

  MRR       - trung binh cua 1/thu hang cua doan dung. No phan biet "doan dung dung dau bang"
              voi "doan dung nam thu tu", dieu ma Recall@k khong phan biet duoc.

Chay: python -m scripts.eval_rag
"""

from app.config import load_settings
from app.db import close_pool
from app.llm.gemini import GeminiClient
from app.rag.retriever import Retriever

# (cau hoi, tieu de muc chua cau tra loi)
# The heading is what identifies the passage: the chunker splits on headings, so each chunk
# begins with the "## ..." line of the section it came from.
# Tieu de muc chinh la thu dinh danh doan van: bo cat doan cat theo tieu de, nen moi doan deu bat
# dau bang dong "## ..." cua muc ma no duoc cat ra.
GOLD: list[tuple[str, str]] = [
    # Quy che dao tao
    ("Dieu kien de duoc xet tot nghiep la gi?", "Dieu kien xet tot nghiep"),
    ("Em can bao nhieu tin chi thi duoc ra truong?", "Dieu kien xet tot nghiep"),
    ("Con mon nao bi diem F thi co tot nghiep duoc khong?", "Dieu kien xet tot nghiep"),
    ("Chuan ngoai ngu de tot nghiep la gi?", "Dieu kien xet tot nghiep"),
    ("Bao nhieu diem thi duoc coi la dat mon?", "Dieu kien dat hoc phan"),
    ("Diem F co duoc tinh vao tin chi tich luy khong?", "Dieu kien dat hoc phan"),
    ("Diem 8.5 thi duoc xep loai chu gi?", "Thang diem va cach quy doi"),
    ("Quy doi tu thang diem 10 sang thang diem 4 nhu the nao?", "Thang diem va cach quy doi"),
    ("GPA tich luy duoc tinh bang cong thuc nao?", "Cach tinh diem trung binh chung tich luy"),
    ("Mot hoc ky em duoc dang ky toi da bao nhieu tin chi?", "Gioi han so tin chi dang ky moi hoc ky"),
    (
        "Sinh vien bi canh bao hoc vu thi duoc dang ky bao nhieu tin?",
        "Gioi han so tin chi dang ky moi hoc ky",
    ),
    ("Khi nao thi bi canh bao hoc vu?", "Canh bao hoc vu"),
    ("Bi canh bao hoc vu may lan thi bi buoc thoi hoc?", "Canh bao hoc vu"),
    ("Mon tien quyet nghia la gi?", "Hoc phan tien quyet"),
    (
        "Em dang hoc mon tien quyet trong ky nay thi co dang ky mon sau duoc khong?",
        "Hoc phan tien quyet",
    ),
    ("Truot mon thi co phai hoc lai khong?", "Hoc lai va hoc cai thien"),
    ("Hoc cai thien thi lay diem lan nao?", "Hoc lai va hoc cai thien"),
    ("Tot nghiep loai gioi can GPA bao nhieu?", "Xep loai tot nghiep"),
    # Chuong trinh dao tao
    ("Mon Tri tue nhan tao co nhung mon tien quyet nao?", "Bang tong hop hoc phan tien quyet"),
    ("Muon hoc Hoc may thi phai dat nhung mon nao truoc?", "Bang tong hop hoc phan tien quyet"),
    ("Mon Co so du lieu yeu cau hoc phan tien quyet gi?", "Bang tong hop hoc phan tien quyet"),
    ("Mon nao khong co hoc phan tien quyet?", "Bang tong hop hoc phan tien quyet"),
    ("Mon Cau truc du lieu va giai thuat co bao nhieu tin chi?", "Khoi kien thuc co so nganh"),
    ("Giai tich 1 bao nhieu tin chi?", "Khoi kien thuc toan va khoa hoc co ban"),
    ("Nhung mon nao thuoc khoi tu chon cua nganh?", "Khoi kien thuc chuyen nganh tu chon"),
    # Huong dan dang ky hoc phan
    ("Quy trinh dang ky mot lop hoc phan gom may buoc?", "Quy trinh dang ky mot lop hoc phan"),
    ("Phieu dang ky co hieu luc trong bao lau?", "Quy trinh dang ky mot lop hoc phan"),
    ("Nhung truong hop nao thi bi tu choi dang ky?", "Cac truong hop dang ky bi tu choi"),
    ("Hai lop nhu the nao thi bi coi la trung lich?", "Quy dinh ve trung lich"),
    ("Lop da du si so thi con dang ky duoc khong?", "Si so lop hoc phan"),
    ("Hoc phi mot tin chi la bao nhieu tien?", "Hoc phi"),
    ("Em co huy dang ky mon duoc khong?", "Huy dang ky"),
]

TOP_K = 4


def chunk_heading(content: str) -> str:
    """The heading a chunk came from, read off its first line.

    Tieu de muc ma mot doan van duoc cat ra, doc tu chinh dong dau tien cua no.
    """
    first_line = content.strip().splitlines()[0] if content.strip() else ""
    return first_line.lstrip("#").strip()


def normalise(text: str) -> str:
    """Strip Vietnamese diacritics so a question written without them still matches.

    Bo dau tieng Viet de mot cau hoi go khong dau van doi chieu duoc voi tieu de co dau.
    """
    pairs = (
        ("aăâáàảãạắằẳẵặấầẩẫậ", "a"),
        ("eêéèẻẽẹếềểễệ", "e"),
        ("iíìỉĩị", "i"),
        ("oôơóòỏõọốồổỗộớờởỡợ", "o"),
        ("uưúùủũụứừửữự", "u"),
        ("yýỳỷỹỵ", "y"),
        ("đ", "d"),
    )
    result = text.lower()
    for sources, target in pairs:
        for source in sources:
            result = result.replace(source, target)
    return result


def main() -> None:
    settings = load_settings()
    retriever = Retriever(GeminiClient(settings))
    loaded = retriever.load()

    if loaded == 0:
        print("Kho tri thuc rong. Hay chay: python -m scripts.ingest")
        return

    print(f"Da nap {loaded} doan tai lieu. Danh gia tren {len(GOLD)} cau hoi, top_k={TOP_K}.\n")

    hits_at_1 = 0
    hits_at_k = 0
    reciprocal_ranks: list[float] = []
    misses: list[tuple[str, str, str]] = []

    for question, expected in GOLD:
        results = retriever.search(question, top_k=TOP_K)
        headings = [normalise(chunk_heading(r.content)) for r in results]
        wanted = normalise(expected)

        rank = next((i + 1 for i, heading in enumerate(headings) if heading == wanted), None)

        if rank == 1:
            hits_at_1 += 1
        if rank is not None:
            hits_at_k += 1
            reciprocal_ranks.append(1 / rank)
        else:
            reciprocal_ranks.append(0.0)
            top = chunk_heading(results[0].content) if results else "(khong co ket qua)"
            misses.append((question, expected, top))

    total = len(GOLD)
    print(f"  Recall@1 : {hits_at_1 / total:.1%}  ({hits_at_1}/{total})")
    print(f"  Recall@{TOP_K} : {hits_at_k / total:.1%}  ({hits_at_k}/{total})")
    print(f"  MRR      : {sum(reciprocal_ranks) / total:.3f}")

    if misses:
        print(f"\n{len(misses)} cau khong tim thay doan dung trong top {TOP_K}:")
        for question, expected, top in misses:
            print(f"\n  Cau hoi   : {question}")
            print(f"  Can lay   : {expected}")
            print(f"  Lay nham  : {top}")
    else:
        print(f"\nMoi cau hoi deu lay dung doan tai lieu trong top {TOP_K}.")

    close_pool()


if __name__ == "__main__":
    main()
