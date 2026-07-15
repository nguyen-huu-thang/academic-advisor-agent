"""Refresh tokens: rotation, reuse detection, and revocation.

Refresh token: xoay vong, phat hien tai su dung, va thu hoi.

Two tokens, two opposite trades, both deliberate.

The access token is a JWT that is stored nowhere. It is checked by its signature alone, so
serving a request costs no database round trip. The price is that it cannot be taken back before
it expires - which is exactly why it is given only fifteen minutes to live. A revoked student
keeps working for at most those fifteen minutes, and that bounded window is the whole price of
not touching the database on the hot path.

The refresh token lives for weeks, so it must be revocable, and a thing can only be revoked if
somewhere a row says it is still valid. It is presented once every fifteen minutes rather than on
every request, so that lookup costs nothing that matters.

Hai loai token, hai danh doi nguoc nhau, ca hai deu co chu dich.

Access token la mot JWT khong duoc luu o dau ca. No chi duoc kiem tra bang chu ky, nen phuc vu
mot request khong ton mot vong goi database nao. Cai gia phai tra la khong the rut no lai truoc
han - va do chinh la ly do no chi duoc cho song muoi lam phut. Mot sinh vien da bi thu hoi quyen
van dung duoc nhieu nhat la muoi lam phut do, va cai cua so co gioi han ay chinh la toan bo cai
gia cua viec khong cham vao database o duong di nong nhat.

Refresh token thi song hang tuan, nen bat buoc phai thu hoi duoc, ma mot thu chi thu hoi duoc khi
o dau do co mot dong ghi rang no van con hieu luc. No lai chi duoc trinh ra muoi lam phut mot lan
chu khong phai moi request mot lan, nen phep tra cuu do khong ton gi dang ke.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from app.config import Settings
from app.db import get_connection

# 256 bits of randomness. This is the number that makes the design work: a token this size cannot
# be guessed, which is why it needs no slow hash and no salt.
# 256 bit ngau nhien. Chinh con so nay lam cho ca thiet ke chay duoc: mot token lon nhu vay thi
# khong doan ra duoc, va do la ly do no khong can ham bam cham va khong can salt.
TOKEN_BYTES = 32

STATUS_ACTIVE = "active"
STATUS_ROTATED = "rotated"
STATUS_REVOKED = "revoked"


class InvalidRefreshToken(Exception):
    """The refresh token is unknown, expired, already used, or revoked.

    Refresh token khong ton tai, da het han, da dung roi, hoac da bi thu hoi.
    """


class RefreshTokenReused(InvalidRefreshToken):
    """A token that had already been rotated was presented again.

    Mot token da duoc xoay vong roi lai duoc trinh ra lan nua.

    Raised separately from a plain invalid token because it means something different, and calls
    for something different: an unknown token is noise, this is an alarm. By the time it is
    raised, the whole family has already been revoked.
    Duoc nem ra rieng khoi mot token khong hop le thong thuong, boi no co nghia khac han, va doi
    hoi mot phan ung khac: mot token la thi chi la nhieu, con day la mot bao dong. Den luc no duoc
    nem ra thi ca ho token da bi thu hoi roi.
    """


@dataclass(frozen=True)
class RotatedTokens:
    student_id: str
    refresh_token: str


def hash_token(raw_token: str) -> str:
    """The value actually stored. The token itself never reaches the database.

    Gia tri thuc su duoc luu. Ban than token khong bao gio di toi database.

    A single SHA-256, not scrypt, and that is a decision rather than an omission. scrypt is slow
    on purpose because a password is short and guessable, and every guess must be made to hurt. A
    refresh token is 256 random bits, so nobody is guessing it at all; a slow hash would buy
    nothing and would put a delay on every single refresh.

    What this hash is for is narrower: if the table is stolen, the rows in it cannot be presented
    to the service as tokens. Making a stolen table useless and making a stolen token hard to
    guess are two different jobs, and only the first one is needed here.

    Mot lan SHA-256, khong phai scrypt, va do la mot quyet dinh chu khong phai mot thieu sot.
    scrypt co y cham vi mat khau thi ngan va doan duoc, nen phai lam cho moi lan doan deu dau don.
    Refresh token la 256 bit ngau nhien, nen khong ai doan no ca; mot ham bam cham chang mua duoc
    gi ma con dat mot khoang cho len moi lan refresh.

    Cong dung cua ban bam nay hep hon nhieu: neu bang bi danh cap, cac dong trong do khong the dem
    trinh cho dich vu nhu mot token. Lam mot cai bang bi cap tro nen vo dung, va lam mot cai token
    bi cap tro nen kho doan, la hai viec khac nhau, va o day chi can viec thu nhat.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_for_new_login(student_id: str, settings: Settings) -> str:
    """Start a fresh family for a student who has just proved their password.

    Mo mot ho token moi cho sinh vien vua chung minh dung mat khau.

    Each login starts its own family, so signing in on a phone does not disturb the session on a
    laptop, and revoking one does not revoke the other.
    Moi lan dang nhap mo mot ho rieng, nen dang nhap tren dien thoai khong lam phien phien lam viec
    tren may tinh, va thu hoi cai nay khong thu hoi cai kia.
    """
    with get_connection() as conn:
        return _mint(conn, student_id, uuid.uuid4().hex, settings)


def rotate(raw_token: str, settings: Settings) -> RotatedTokens:
    """Spend a refresh token and hand back its replacement.

    Tieu mot refresh token va tra ve token thay the no.

    The old token is dead the moment this succeeds. That is the point of rotation: a refresh
    token stolen from the wire is only good until its rightful owner next uses theirs, instead of
    being good for the next fortnight.
    Token cu chet ngay khi ham nay thanh cong. Do chinh la muc dich cua viec xoay vong: mot refresh
    token bi danh cap tren duong truyen chi con gia tri cho toi lan tiep theo chu that su cua no
    dung token cua ho, thay vi con gia tri suot hai tuan toi.
    """
    token_hash = hash_token(raw_token)

    with get_connection() as conn:
        # Claim the token by the very same UPDATE that checks it, exactly as a registration slip
        # is claimed. Two refreshes racing with the same token cannot both win: the second one
        # updates no row, and falls through to _reject below - where being unable to claim an
        # already-rotated token is precisely how a replay is caught.
        # Gianh lay token bang chinh cau UPDATE dang kiem tra no, y het cach mot phieu dang ky duoc
        # gianh. Hai lan refresh chay dua voi cung mot token khong the cung thang: cau thu hai
        # khong cap nhat duoc dong nao, va roi xuong _reject ben duoi - noi ma viec khong gianh
        # duoc mot token da bi xoay vong chinh la cach mot lan dung lai bi bat.
        claimed = conn.execute(
            f"""
            UPDATE refresh_tokens
            SET status = '{STATUS_ROTATED}'
            WHERE token_hash = %s AND status = '{STATUS_ACTIVE}' AND expires_at > now()
            RETURNING student_id, family_id
            """,
            (token_hash,),
        ).fetchone()

        if claimed is None:
            # Written and committed inside this block, then raised outside it. Raising here would
            # roll the revocation back, and the family would survive the very alarm that was
            # supposed to kill it.
            # Duoc ghi va commit ben trong khoi nay, roi moi nem ra ben ngoai. Neu nem ngay tai day
            # thi viec thu hoi se bi cuon nguoc lai, va ca ho token se song sot qua dung cai bao
            # dong le ra phai giet no.
            failure = _reject(conn, token_hash)
        else:
            failure = None
            student_id = claimed["student_id"]
            new_token = _mint(conn, student_id, claimed["family_id"], settings)
            _prune_expired(conn, student_id)

    if failure is not None:
        raise failure

    return RotatedTokens(student_id=student_id, refresh_token=new_token)


def revoke_family_of(raw_token: str) -> None:
    """Log out: kill the token presented, and every token descended from the same login.

    Dang xuat: giet token duoc trinh ra, va moi token sinh ra tu cung lan dang nhap do.

    Revoking the family rather than the single row is what makes logout mean anything. Killing
    only the token in hand would leave its parent - already rotated, but still sitting in the
    table - and a thief holding that parent could carry on refreshing.
    Thu hoi ca ho thay vi chi mot dong la thu lam cho viec dang xuat co y nghia. Neu chi giet token
    dang cam tren tay thi token cha cua no - da bi xoay vong, nhung van con nam trong bang - se
    song sot, va mot ke trom dang giu token cha do van cu the ma refresh tiep.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT family_id FROM refresh_tokens WHERE token_hash = %s",
            (hash_token(raw_token),),
        ).fetchone()
        if row is None:
            # Logging out with a token nobody has ever seen is not an error worth reporting: it
            # leaves the caller logged out either way, which is what they asked for.
            # Dang xuat bang mot token chua ai tung thay khong phai la loi dang bao: du sao nguoi
            # goi cung ket thuc o trang thai da dang xuat, dung nhu ho muon.
            return
        _revoke_family(conn, row["family_id"])


def _mint(conn, student_id: str, family_id: str, settings: Settings) -> str:
    """Create one new refresh token row and return the raw token to hand to the client.

    Tao mot dong refresh token moi va tra ve token GOC de dua cho client.

    Sinh token ngau nhien (256 bit), luu vao bang chi phan bam (hash_token) chu khong luu
    token goc - neu bang bi lo thi cac dong trong do khong the dem trinh nhu token. Han dung
    do PostgreSQL tinh: now() + so_ngay * interval '1 day'. family_id gan token nay vao dung
    ho token cua lan dang nhap: token cap luc dang nhap dung uuid moi, token xoay vong dung
    lai family_id cua token cu (xem rotate) de ca chuoi cung mot ho.
    """
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    conn.execute(
        """
        INSERT INTO refresh_tokens (token_hash, family_id, student_id, expires_at)
        VALUES (%s, %s, %s, now() + %s * interval '1 day')
        """,
        (hash_token(raw_token), family_id, student_id, settings.refresh_token_ttl_days),
    )
    return raw_token


def _reject(conn, token_hash: str) -> InvalidRefreshToken:
    """Work out why the token could not be claimed, and revoke the family if it was a replay.

    Tim ra vi sao token khong gianh duoc, va thu hoi ca ho neu do la mot lan dung lai.
    """
    row = conn.execute(
        """
        SELECT family_id, status, (expires_at <= now()) AS expired
        FROM refresh_tokens WHERE token_hash = %s
        """,
        (token_hash,),
    ).fetchone()

    if row is None:
        return InvalidRefreshToken("Refresh token khong hop le. Hay dang nhap lai.")

    if row["status"] == STATUS_ROTATED:
        # The alarm. This token was spent already, and here it is again. Either a thief copied it
        # and is spending it behind the student's back, or the student is retrying a request whose
        # reply never arrived - and there is no way, from here, to tell those two apart.
        #
        # So assume the worse one. If it was a thief, the family must die or the thief keeps the
        # session. If it was an honest retry, the student is logged out and has to sign in again,
        # which is an annoyance and not a breach. An annoyance is recoverable; a live session in
        # someone else's hands is not.
        #
        # Bao dong. Token nay da bi tieu roi, vay ma no lai xuat hien. Hoac mot ke trom da sao chep
        # no va dang tieu no sau lung sinh vien, hoac sinh vien dang gui lai mot request ma cau tra
        # loi khong bao gio toi noi - va tu day thi khong co cach nao phan biet duoc hai truong hop.
        #
        # Nen cu gia dinh truong hop xau hon. Neu la ke trom, ca ho phai chet, khong thi ke trom
        # giu duoc phien. Neu la mot lan gui lai ngay tinh, sinh vien bi dang xuat va phai dang nhap
        # lai, do la mot su phien toai chu khong phai mot vu xam nhap. Phien toai thi khac phuc duoc;
        # mot phien dang song trong tay nguoi khac thi khong.
        _revoke_family(conn, row["family_id"])
        return RefreshTokenReused(
            "Refresh token nay da duoc su dung roi. Vi ly do an toan, toan bo phien dang nhap "
            "lien quan da bi thu hoi. Hay dang nhap lai."
        )

    if row["status"] == STATUS_REVOKED:
        return InvalidRefreshToken("Phien dang nhap nay da bi thu hoi. Hay dang nhap lai.")

    if row["expired"]:
        return InvalidRefreshToken("Refresh token da het han. Hay dang nhap lai.")

    return InvalidRefreshToken("Refresh token khong hop le. Hay dang nhap lai.")


def _revoke_family(conn, family_id: str) -> None:
    """Mark every still-live token in a family as revoked, in one statement.

    Danh dau moi token con song trong mot ho la da thu hoi, chi bang mot cau lenh.

    Dung khi dang xuat, hoac khi phat hien tai su dung token. Cap nhat tat ca dong cung
    family_id (tru nhung dong da revoked san de khong ghi de vo ich). Sau cau lenh nay, moi
    token thuoc lan dang nhap do - ke ca token dang duoc cam chinh dang - deu khong dung duoc
    nua. STATUS_REVOKED va cac hang khac duoc noi truc tiep vao chuoi SQL vi chung la hang so
    trong code, khong phai du lieu tu nguoi dung, nen khong co rui ro SQL injection.
    """
    conn.execute(
        f"""
        UPDATE refresh_tokens SET status = '{STATUS_REVOKED}'
        WHERE family_id = %s AND status <> '{STATUS_REVOKED}'
        """,
        (family_id,),
    )


def _prune_expired(conn, student_id: str) -> None:
    """Drop this student's rows that have expired. They protect nothing any more.

    Xoa cac dong da het han cua sinh vien nay. Chung khong con bao ve dieu gi nua.

    Rotated rows must be kept while they are alive, because they are what turns a replayed token
    into a recognised replay. Once expired, a replay of them would be refused for being expired
    anyway, so the row has stopped earning its keep.

    Without this the table would grow one row per refresh for ever: a student refreshing every
    fifteen minutes for a year is thirty-five thousand rows that nothing will ever read.

    Cac dong rotated phai duoc giu trong khi chung con song, boi chinh chung bien mot token bi dung
    lai thanh mot lan dung lai bi nhan ra. Khi da het han thi mot lan dung lai cung se bi tu choi vi
    het han roi, nen dong do khong con lam duoc viec gi nua.

    Neu khong co buoc nay, bang se phinh them mot dong sau moi lan refresh, mai mai: mot sinh vien
    cu muoi lam phut refresh mot lan trong mot nam la ba muoi lam nghin dong ma khong ai doc toi.
    """
    conn.execute(
        "DELETE FROM refresh_tokens WHERE student_id = %s AND expires_at <= now()",
        (student_id,),
    )
