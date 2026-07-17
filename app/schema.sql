-- Schema for the academic advisor assistant.
-- Lược đồ cơ sở dữ liệu cho trợ lý cố vấn học tập.

-- Knowledge base: documents split into chunks with their embedding vectors.
-- Kho tri thức: tài liệu được cắt thành các đoạn kèm vector embedding.
CREATE TABLE IF NOT EXISTS documents (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    source      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id           SERIAL PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    content      TEXT NOT NULL,
    -- Embedding is stored already L2-normalised, so cosine similarity is a plain dot product.
    -- Embedding được chuẩn hóa L2 sẵn, nên độ tương đồng cosine chỉ còn là tích vô hướng.
    embedding    DOUBLE PRECISION[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);

-- Simulated student records that the agent tools read from.
-- Dữ liệu sinh viên mô phỏng, là nguồn mà các tool của agent truy vấn.
--
-- academic_status is what the credit ceiling is derived from. A student on academic
-- warning may register for fewer credits, and that ceiling is read from here rather than
-- taken from anything the model says.
-- academic_status là thứ quyết định trần tín chỉ. Sinh viên bị cảnh báo học vụ chỉ được
-- đăng ký ít tín chỉ hơn, và trần đó được đọc từ đây chứ không lấy theo lời model nói.
-- password_hash holds a salted scrypt digest, never the password itself. The empty default
-- exists only so this column can be added to a database that predates it; an empty string
-- parses as no valid hash at all, so a student whose row still carries it can never log in.
-- That is the right way for a half-finished migration to fail: closed, not open.
-- password_hash lưu một bản băm scrypt có salt, không bao giờ lưu mật khẩu. Giá trị mặc định
-- rỗng chỉ tồn tại để cột này thêm được vào một database có trước nó; một chuỗi rỗng thì không
-- đọc ra được bản băm hợp lệ nào, nên sinh viên nào còn mang giá trị đó sẽ không thể đăng nhập.
-- Đó là cách đúng để một lần đổi lược đồ làm dở dang bị thất bại: đóng lại, chứ không mở ra.
CREATE TABLE IF NOT EXISTS students (
    student_id       TEXT PRIMARY KEY,
    full_name        TEXT NOT NULL,
    major            TEXT NOT NULL,
    cohort           TEXT NOT NULL,
    password_hash    TEXT NOT NULL DEFAULT '',
    gpa              NUMERIC(3, 2) NOT NULL DEFAULT 0 CHECK (gpa >= 0 AND gpa <= 4),
    credits_earned   INTEGER NOT NULL DEFAULT 0 CHECK (credits_earned >= 0),
    academic_status  TEXT NOT NULL DEFAULT 'binh_thuong'
                     CHECK (academic_status IN ('binh_thuong', 'canh_bao_1', 'canh_bao_2'))
);

ALTER TABLE students ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS courses (
    course_code  TEXT PRIMARY KEY,
    course_name  TEXT NOT NULL,
    credits      INTEGER NOT NULL CHECK (credits > 0),
    department   TEXT NOT NULL,
    -- Compulsory courses count towards the graduation requirement; electives are chosen.
    -- Môn bắt buộc tính vào điều kiện tốt nghiệp; môn tự chọn thì sinh viên tự chọn.
    is_required  BOOLEAN NOT NULL DEFAULT TRUE
);

-- Which courses must be passed before another course may be taken.
-- Những môn phải đạt trước khi được học một môn khác.
--
-- This table, not the model, decides whether a prerequisite is met. A student can insist
-- they have already taken the prerequisite and the model may well believe them; the join
-- against `grades` does not.
-- Chính bảng này quyết định môn tiên quyết đã đạt hay chưa, không phải model. Sinh viên có
-- thể một mực khẳng định mình đã học môn tiên quyết và model hoàn toàn có thể tin theo;
-- còn phép join với bảng `grades` thì không.
CREATE TABLE IF NOT EXISTS prerequisites (
    course_code  TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    prereq_code  TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    PRIMARY KEY (course_code, prereq_code),
    -- A course cannot require itself, which would make it impossible to ever register for.
    -- Một môn không thể tự làm tiên quyết của chính nó, nếu không sẽ không bao giờ đăng ký được.
    CHECK (course_code <> prereq_code)
);

-- A class a student can actually register for: one course, one timetable slot, one room.
-- Một lớp học phần sinh viên có thể đăng ký: một môn, một khung giờ, một phòng.
--
-- `enrolled` is a counter kept next to `capacity` so the two can be compared under a single
-- row lock at registration time. The CHECK below is the last line of defence: even if the
-- application logic were wrong, PostgreSQL would still refuse to overfill the class.
-- `enrolled` là bộ đếm đặt cạnh `capacity` để hai giá trị này được so sánh dưới cùng một
-- khóa dòng lúc đăng ký. Ràng buộc CHECK bên dưới là lớp phòng thủ cuối cùng: dù logic ứng
-- dụng có sai, PostgreSQL vẫn không cho lớp vượt sĩ số.
CREATE TABLE IF NOT EXISTS class_sections (
    id            SERIAL PRIMARY KEY,
    course_code   TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    section_no    TEXT NOT NULL,
    semester      TEXT NOT NULL,
    lecturer      TEXT NOT NULL,
    capacity      INTEGER NOT NULL CHECK (capacity > 0),
    enrolled      INTEGER NOT NULL DEFAULT 0,
    -- 2 = Monday ... 8 = Sunday, following how Vietnamese timetables are written.
    -- 2 = thứ Hai ... 8 = Chủ nhật, theo cách thời khóa biểu Việt Nam vẫn ghi.
    day_of_week   INTEGER NOT NULL CHECK (day_of_week BETWEEN 2 AND 8),
    start_period  INTEGER NOT NULL CHECK (start_period BETWEEN 1 AND 12),
    end_period    INTEGER NOT NULL CHECK (end_period BETWEEN 1 AND 12),
    room          TEXT NOT NULL,
    UNIQUE (course_code, section_no, semester),
    CHECK (end_period >= start_period),
    CHECK (enrolled >= 0 AND enrolled <= capacity)
);

CREATE INDEX IF NOT EXISTS idx_sections_course ON class_sections(course_code, semester);

-- Grades already recorded. `passed` is stored rather than recomputed from `score` so that
-- the pass rule lives in one place and the prerequisite check is a plain lookup.
-- Điểm đã có. Cột `passed` được lưu sẵn thay vì tính lại từ `score`, để quy tắc đạt môn chỉ
-- nằm ở một chỗ và việc kiểm tra tiên quyết chỉ còn là một phép tra cứu đơn giản.
CREATE TABLE IF NOT EXISTS grades (
    id           SERIAL PRIMARY KEY,
    student_id   TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    course_code  TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    semester     TEXT NOT NULL,
    score        NUMERIC(4, 2) NOT NULL CHECK (score >= 0 AND score <= 10),
    passed       BOOLEAN NOT NULL,
    UNIQUE (student_id, course_code, semester)
);

CREATE INDEX IF NOT EXISTS idx_grades_student ON grades(student_id, course_code);

-- A registration that has actually gone through.
-- Một lệnh đăng ký đã thực sự thành công.
--
-- course_code is denormalised from class_sections purely so the second UNIQUE below can
-- exist: registering for two different sections of the same course in one semester is a
-- mistake the database should refuse, not something the application has to remember to check.
-- course_code được lặp lại từ class_sections chỉ để ràng buộc UNIQUE thứ hai bên dưới tồn tại
-- được: đăng ký hai lớp khác nhau của cùng một môn trong một kỳ là lỗi mà database nên tự từ
-- chối, chứ không phải thứ mà ứng dụng phải nhớ để kiểm tra.
CREATE TABLE IF NOT EXISTS enrollments (
    id                SERIAL PRIMARY KEY,
    student_id        TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    class_section_id  INTEGER NOT NULL REFERENCES class_sections(id) ON DELETE CASCADE,
    course_code       TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    semester          TEXT NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, class_section_id),
    UNIQUE (student_id, course_code, semester)
);

CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments(student_id, semester);

-- When students are allowed to register at all.
-- Khoảng thời gian sinh viên được phép đăng ký học phần.
CREATE TABLE IF NOT EXISTS registration_windows (
    semester   TEXT PRIMARY KEY,
    opens_at   TIMESTAMPTZ NOT NULL,
    closes_at  TIMESTAMPTZ NOT NULL,
    CHECK (closes_at > opens_at)
);

-- Conversation memory. Scoped by student as well as by session, so guessing another
-- person's session id does not reveal their conversation.
-- Bộ nhớ hội thoại. Được giới hạn theo cả sinh viên lẫn phiên, nên đoán trúng session id
-- của người khác cũng không đọc được hội thoại của họ.
CREATE TABLE IF NOT EXISTS messages (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL,
    student_id  TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'model')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, student_id, id);

-- A registration the assistant has prepared but not yet carried out.
-- Một lệnh đăng ký trợ lý đã chuẩn bị nhưng chưa thực hiện.
--
-- This table is what makes "the student confirmed" a fact instead of a claim. The model
-- cannot set a flag to assert consent: it can only create a row here, read the class back
-- to the student, and wait. The row records which turn created it, and the guardrail
-- refuses to execute a row that was created in the turn currently running - so consent
-- always costs the student a separate message that the model cannot fabricate.
-- Bảng này biến "sinh viên đã xác nhận" từ một lời khai thành một sự thật. Model không thể
-- đặt một cờ để tự khẳng định là sinh viên đã đồng ý: nó chỉ có thể tạo một dòng ở đây, đọc
-- lại thông tin lớp cho sinh viên nghe, rồi chờ. Dòng này ghi lại lượt nào đã tạo ra nó, và
-- guardrail từ chối thực thi một dòng được tạo ra trong chính lượt đang chạy - nên sự đồng ý
-- luôn phải trả bằng một tin nhắn riêng của sinh viên, thứ mà model không bịa ra được.
CREATE TABLE IF NOT EXISTS pending_registrations (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    student_id        TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    -- The turn that created this row. Confirming inside the same turn is refused.
    -- Lượt đã tạo ra dòng này. Xác nhận ngay trong cùng lượt đó sẽ bị từ chối.
    created_turn_id   TEXT NOT NULL,
    class_section_id  INTEGER NOT NULL REFERENCES class_sections(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'cho_xac_nhan'
                      CHECK (status IN ('cho_xac_nhan', 'da_thuc_hien')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_registrations(session_id, student_id);

-- Refresh tokens, stored so they can be taken away again.
--
-- The access token is a JWT and is deliberately not stored anywhere: it is checked by its
-- signature alone, so serving a request costs no database round trip. The price of that is that
-- it cannot be withdrawn before it expires, which is why it only lives fifteen minutes.
--
-- The refresh token is the opposite trade, and on purpose. It lives for weeks, so it MUST be
-- withdrawable, and a thing can only be withdrawn if somewhere there is a row saying it is still
-- valid. It is presented rarely - once every fifteen minutes, not once a request - so the lookup
-- costs nothing that matters.
--
-- Stateless where it is hot, stateful where it must be revocable.
--
-- Refresh token, được lưu lại để có thể thu hồi.
--
-- Access token là một JWT và cố ý không được lưu ở đâu cả: nó chỉ được kiểm tra bằng chữ ký, nên
-- phục vụ một request không tốn một vòng gọi database nào. Cái giá phải trả là không thể rút nó
-- lại trước hạn, và đó là lý do nó chỉ sống mười lăm phút.
--
-- Refresh token thì đánh đổi ngược lại, và đó là cố ý. Nó sống hàng tuần, nên BẮT BUỘC phải rút
-- lại được, mà một thứ chỉ rút lại được khi ở đâu đó có một dòng ghi rằng nó vẫn còn hiệu lực. Nó
-- lại được trình ra rất thưa thớt - mười lăm phút một lần, không phải mỗi request một lần - nên
-- phép tra cứu này không tốn gì đáng kể.
--
-- Không trạng thái ở chỗ nóng, có trạng thái ở chỗ bắt buộc phải thu hồi được.
CREATE TABLE IF NOT EXISTS refresh_tokens (
    -- The token itself is never stored, only its SHA-256. If this table leaked, the rows in it
    -- still could not be presented to the service as tokens.
    --
    -- No salt, and no scrypt, and that is not an oversight. A password is short and guessable, so
    -- it must be made expensive to guess. A refresh token is 256 random bits: it cannot be
    -- guessed at all, so a slow hash would buy nothing and cost a delay on every refresh. The
    -- hash here exists to make a stolen *table* useless, not to make a stolen *token* hard to
    -- find - those are different jobs.
    --
    -- Bản thân token không bao giờ được lưu, chỉ lưu SHA-256 của nó. Nếu bảng này bị lộ, các dòng
    -- trong đó vẫn không thể đem trình cho dịch vụ như một token.
    --
    -- Không salt, không scrypt, và đó không phải là sơ suất. Mật khẩu thì ngắn và đoán được, nên
    -- phải làm cho việc đoán trở nên đắt đỏ. Refresh token thì là 256 bit ngẫu nhiên: không đoán
    -- được, nên một hàm băm chậm chẳng mua được gì mà còn làm mỗi lần refresh phải chờ thêm. Bản
    -- băm ở đây tồn tại để một cái BẢNG bị đánh cắp trở nên vô dụng, chứ không phải để một cái
    -- TOKEN bị đánh cắp trở nên khó tìm - đó là hai việc khác nhau.
    token_hash  TEXT PRIMARY KEY,

    -- Every token minted from one login shares a family id: the token handed out at login, the
    -- one that replaced it, the one that replaced that, and so on. The family is what gets
    -- revoked when a used token turns up again, because at that point one of the two holders is
    -- a thief and there is no way to tell which.
    -- Mọi token sinh ra từ một lần đăng nhập đều chung một mã "họ": token cấp lúc đăng nhập, token
    -- thay thế nó, token thay thế token đó, và cứ thế. Chính cả họ này sẽ bị thu hồi khi một token
    -- đã dùng lại xuất hiện lần nữa, bởi lúc đó một trong hai người đang cầm token là kẻ trộm, và
    -- không có cách nào biết là ai.
    family_id   TEXT NOT NULL,

    student_id  TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,

    -- 'rotated' rows are kept, not deleted, until they expire. They have to be: a rotated row is
    -- the only evidence that lets a replayed token be recognised as a replay instead of as a
    -- token nobody has ever seen.
    -- Các dòng 'rotated' được GIỮ LẠI, không xóa, cho tới khi hết hạn. Bắt buộc phải vậy: một dòng
    -- rotated chính là bằng chứng duy nhất cho phép nhận ra một token bị dùng lại là một lần dùng
    -- lại, thay vì là một token chưa ai từng thấy.
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'rotated', 'revoked')),

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refresh_family ON refresh_tokens(family_id);
CREATE INDEX IF NOT EXISTS idx_refresh_student ON refresh_tokens(student_id, expires_at);

-- Audit trail of every tool call the agent makes. The school must be able to answer
-- "what did the agent actually do", so this is written before the result is returned.
-- Nhật ký mọi lần agent gọi tool. Nhà trường phải trả lời được "agent đã làm gì",
-- nên bản ghi này được lưu trước khi trả kết quả về.
CREATE TABLE IF NOT EXISTS tool_audit_log (
    id           SERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    student_id   TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    arguments    JSONB NOT NULL,
    allowed      BOOLEAN NOT NULL,
    denial_note  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON tool_audit_log(session_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_student ON tool_audit_log(student_id, id);
