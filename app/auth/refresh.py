"""Refresh tokens: rotation, reuse detection, and revocation.

Refresh token: xoay vòng, phát hiện tái sử dụng, và thu hồi.

Two tokens, two opposite trades, both deliberate.

The access token is a JWT that is stored nowhere. It is checked by its signature alone, so
serving a request costs no database round trip. The price is that it cannot be taken back before
it expires - which is exactly why it is given only fifteen minutes to live. A revoked student
keeps working for at most those fifteen minutes, and that bounded window is the whole price of
not touching the database on the hot path.

The refresh token lives for weeks, so it must be revocable, and a thing can only be revoked if
somewhere a row says it is still valid. It is presented once every fifteen minutes rather than on
every request, so that lookup costs nothing that matters.

Hai loại token, hai đánh đổi ngược nhau, cả hai đều có chủ đích.

Access token là một JWT không được lưu ở đâu cả. Nó chỉ được kiểm tra bằng chữ ký, nên phục vụ
một request không tốn một vòng gọi database nào. Cái giá phải trả là không thể rút nó lại trước
hạn - và đó chính là lý do nó chỉ được cho sống mười lăm phút. Một sinh viên đã bị thu hồi quyền
vẫn dùng được nhiều nhất là mười lăm phút đó, và cái cửa sổ có giới hạn ấy chính là toàn bộ cái
giá của việc không chạm vào database ở đường đi nóng nhất.

Refresh token thì sống hàng tuần, nên bắt buộc phải thu hồi được, mà một thứ chỉ thu hồi được khi
ở đâu đó có một dòng ghi rằng nó vẫn còn hiệu lực. Nó lại chỉ được trình ra mười lăm phút một lần
chứ không phải mỗi request một lần, nên phép tra cứu đó không tốn gì đáng kể.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from app.config import Settings
from app.db import get_connection

# 256 bits of randomness. This is the number that makes the design work: a token this size cannot
# be guessed, which is why it needs no slow hash and no salt.
# 256 bit ngẫu nhiên. Chính con số này làm cho cả thiết kế chạy được: một token lớn như vậy thì
# không đoán ra được, và đó là lý do nó không cần hàm băm chậm và không cần salt.
TOKEN_BYTES = 32

STATUS_ACTIVE = "active"
STATUS_ROTATED = "rotated"
STATUS_REVOKED = "revoked"


class InvalidRefreshToken(Exception):
    """The refresh token is unknown, expired, already used, or revoked.

    Refresh token không tồn tại, đã hết hạn, đã dùng rồi, hoặc đã bị thu hồi.
    """


class RefreshTokenReused(InvalidRefreshToken):
    """A token that had already been rotated was presented again.

    Một token đã được xoay vòng rồi lại được trình ra lần nữa.

    Raised separately from a plain invalid token because it means something different, and calls
    for something different: an unknown token is noise, this is an alarm. By the time it is
    raised, the whole family has already been revoked.
    Được ném ra riêng khỏi một token không hợp lệ thông thường, bởi nó có nghĩa khác hẳn, và đòi
    hỏi một phản ứng khác: một token lạ thì chỉ là nhiễu, còn đây là một báo động. Đến lúc nó được
    ném ra thì cả họ token đã bị thu hồi rồi.
    """


@dataclass(frozen=True)
class RotatedTokens:
    student_id: str
    refresh_token: str


def hash_token(raw_token: str) -> str:
    """The value actually stored. The token itself never reaches the database.

    Giá trị thực sự được lưu. Bản thân token không bao giờ đi tới database.

    A single SHA-256, not scrypt, and that is a decision rather than an omission. scrypt is slow
    on purpose because a password is short and guessable, and every guess must be made to hurt. A
    refresh token is 256 random bits, so nobody is guessing it at all; a slow hash would buy
    nothing and would put a delay on every single refresh.

    What this hash is for is narrower: if the table is stolen, the rows in it cannot be presented
    to the service as tokens. Making a stolen table useless and making a stolen token hard to
    guess are two different jobs, and only the first one is needed here.

    Một lần SHA-256, không phải scrypt, và đó là một quyết định chứ không phải một thiếu sót.
    scrypt cố ý chậm vì mật khẩu thì ngắn và đoán được, nên phải làm cho mỗi lần đoán đều đau đớn.
    Refresh token là 256 bit ngẫu nhiên, nên không ai đoán nó cả; một hàm băm chậm chẳng mua được
    gì mà còn đặt một khoảng chờ lên mỗi lần refresh.

    Công dụng của bản băm này hẹp hơn nhiều: nếu bảng bị đánh cắp, các dòng trong đó không thể đem
    trình cho dịch vụ như một token. Làm một cái bảng bị cắp trở nên vô dụng, và làm một cái token
    bị cắp trở nên khó đoán, là hai việc khác nhau, và ở đây chỉ cần việc thứ nhất.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_for_new_login(student_id: str, settings: Settings) -> str:
    """Start a fresh family for a student who has just proved their password.

    Mở một họ token mới cho sinh viên vừa chứng minh đúng mật khẩu.

    Each login starts its own family, so signing in on a phone does not disturb the session on a
    laptop, and revoking one does not revoke the other.
    Mỗi lần đăng nhập mở một họ riêng, nên đăng nhập trên điện thoại không làm phiền phiên làm việc
    trên máy tính, và thu hồi cái này không thu hồi cái kia.
    """
    with get_connection() as conn:
        return _mint(conn, student_id, uuid.uuid4().hex, settings)


def rotate(raw_token: str, settings: Settings) -> RotatedTokens:
    """Spend a refresh token and hand back its replacement.

    Tiêu một refresh token và trả về token thay thế nó.

    The old token is dead the moment this succeeds. That is the point of rotation: a refresh
    token stolen from the wire is only good until its rightful owner next uses theirs, instead of
    being good for the next fortnight.
    Token cũ chết ngay khi hàm này thành công. Đó chính là mục đích của việc xoay vòng: một refresh
    token bị đánh cắp trên đường truyền chỉ còn giá trị cho tới lần tiếp theo chủ thật sự của nó
    dùng token của họ, thay vì còn giá trị suốt hai tuần tới.
    """
    token_hash = hash_token(raw_token)

    with get_connection() as conn:
        # Claim the token by the very same UPDATE that checks it, exactly as a registration slip
        # is claimed. Two refreshes racing with the same token cannot both win: the second one
        # updates no row, and falls through to _reject below - where being unable to claim an
        # already-rotated token is precisely how a replay is caught.
        # Giành lấy token bằng chính câu UPDATE đang kiểm tra nó, y hệt cách một phiếu đăng ký được
        # giành. Hai lần refresh chạy đua với cùng một token không thể cùng thắng: câu thứ hai
        # không cập nhật được dòng nào, và rơi xuống _reject bên dưới - nơi mà việc không giành
        # được một token đã bị xoay vòng chính là cách một lần dùng lại bị bắt.
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
            # Được ghi và commit bên trong khối này, rồi mới ném ra bên ngoài. Nếu ném ngay tại đây
            # thì việc thu hồi sẽ bị cuốn ngược lại, và cả họ token sẽ sống sót qua đúng cái báo
            # động lẽ ra phải giết nó.
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

    Đăng xuất: giết token được trình ra, và mọi token sinh ra từ cùng lần đăng nhập đó.

    Revoking the family rather than the single row is what makes logout mean anything. Killing
    only the token in hand would leave its parent - already rotated, but still sitting in the
    table - and a thief holding that parent could carry on refreshing.
    Thu hồi cả họ thay vì chỉ một dòng là thứ làm cho việc đăng xuất có ý nghĩa. Nếu chỉ giết token
    đang cầm trên tay thì token cha của nó - đã bị xoay vòng, nhưng vẫn còn nằm trong bảng - sẽ
    sống sót, và một kẻ trộm đang giữ token cha đó vẫn cứ thế mà refresh tiếp.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT family_id FROM refresh_tokens WHERE token_hash = %s",
            (hash_token(raw_token),),
        ).fetchone()
        if row is None:
            # Logging out with a token nobody has ever seen is not an error worth reporting: it
            # leaves the caller logged out either way, which is what they asked for.
            # Đăng xuất bằng một token chưa ai từng thấy không phải là lỗi đáng báo: dù sao người
            # gọi cũng kết thúc ở trạng thái đã đăng xuất, đúng như họ muốn.
            return
        _revoke_family(conn, row["family_id"])


def _mint(conn, student_id: str, family_id: str, settings: Settings) -> str:
    """Create one new refresh token row and return the raw token to hand to the client.

    Tạo một dòng refresh token mới và trả về token GỐC để đưa cho client.

    Sinh token ngẫu nhiên (256 bit), lưu vào bảng chỉ phần băm (hash_token) chứ không lưu
    token gốc - nếu bảng bị lộ thì các dòng trong đó không thể đem trình như token. Hạn dùng
    do PostgreSQL tính: now() + so_ngay * interval '1 day'. family_id gắn token này vào đúng
    họ token của lần đăng nhập: token cấp lúc đăng nhập dùng uuid mới, token xoay vòng dùng
    lại family_id của token cũ (xem rotate) để cả chuỗi cùng một họ.
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

    Tìm ra vì sao token không giành được, và thu hồi cả họ nếu đó là một lần dùng lại.
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
        # Báo động. Token này đã bị tiêu rồi, vậy mà nó lại xuất hiện. Hoặc một kẻ trộm đã sao chép
        # nó và đang tiêu nó sau lưng sinh viên, hoặc sinh viên đang gửi lại một request mà câu trả
        # lời không bao giờ tới nơi - và từ đây thì không có cách nào phân biệt được hai trường hợp.
        #
        # Nên cứ giả định trường hợp xấu hơn. Nếu là kẻ trộm, cả họ phải chết, không thì kẻ trộm
        # giữ được phiên. Nếu là một lần gửi lại ngay tình, sinh viên bị đăng xuất và phải đăng nhập
        # lại, đó là một sự phiền toái chứ không phải một vụ xâm nhập. Phiền toái thì khắc phục được;
        # một phiên đang sống trong tay người khác thì không.
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

    Đánh dấu mọi token còn sống trong một họ là đã thu hồi, chỉ bằng một câu lệnh.

    Dùng khi đăng xuất, hoặc khi phát hiện tái sử dụng token. Cập nhật tất cả dòng cùng
    family_id (trừ những dòng đã revoked sẵn để không ghi đè vô ích). Sau câu lệnh này, mọi
    token thuộc lần đăng nhập đó - kể cả token đang được cầm chính đáng - đều không dùng được
    nữa. STATUS_REVOKED và các hằng khác được nối trực tiếp vào chuỗi SQL vì chúng là hằng số
    trong code, không phải dữ liệu từ người dùng, nên không có rủi ro SQL injection.
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

    Xóa các dòng đã hết hạn của sinh viên này. Chúng không còn bảo vệ điều gì nữa.

    Rotated rows must be kept while they are alive, because they are what turns a replayed token
    into a recognised replay. Once expired, a replay of them would be refused for being expired
    anyway, so the row has stopped earning its keep.

    Without this the table would grow one row per refresh for ever: a student refreshing every
    fifteen minutes for a year is thirty-five thousand rows that nothing will ever read.

    Các dòng rotated phải được giữ trong khi chúng còn sống, bởi chính chúng biến một token bị dùng
    lại thành một lần dùng lại bị nhận ra. Khi đã hết hạn thì một lần dùng lại cũng sẽ bị từ chối vì
    hết hạn rồi, nên dòng đó không còn làm được việc gì nữa.

    Nếu không có bước này, bảng sẽ phình thêm một dòng sau mỗi lần refresh, mãi mãi: một sinh viên
    cứ mười lăm phút refresh một lần trong một năm là ba mươi lăm nghìn dòng mà không ai đọc tới.
    """
    conn.execute(
        "DELETE FROM refresh_tokens WHERE student_id = %s AND expires_at <= now()",
        (student_id,),
    )
