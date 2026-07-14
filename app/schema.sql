-- Schema for the academic advisor assistant.
-- Luoc do co so du lieu cho tro ly co van hoc tap.

-- Knowledge base: documents split into chunks with their embedding vectors.
-- Kho tri thuc: tai lieu duoc cat thanh cac doan kem vector embedding.
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
    -- Embedding duoc chuan hoa L2 san, nen do tuong dong cosine chi con la tich vo huong.
    embedding    DOUBLE PRECISION[] NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);

-- Simulated student records that the agent tools read from.
-- Du lieu sinh vien mo phong, la nguon ma cac tool cua agent truy van.
--
-- academic_status is what the credit ceiling is derived from. A student on academic
-- warning may register for fewer credits, and that ceiling is read from here rather than
-- taken from anything the model says.
-- academic_status la thu quyet dinh tran tin chi. Sinh vien bi canh bao hoc vu chi duoc
-- dang ky it tin chi hon, va tran do duoc doc tu day chu khong lay theo loi model noi.
-- password_hash holds a salted scrypt digest, never the password itself. The empty default
-- exists only so this column can be added to a database that predates it; an empty string
-- parses as no valid hash at all, so a student whose row still carries it can never log in.
-- That is the right way for a half-finished migration to fail: closed, not open.
-- password_hash luu mot ban bam scrypt co salt, khong bao gio luu mat khau. Gia tri mac dinh
-- rong chi ton tai de cot nay them duoc vao mot database co truoc no; mot chuoi rong thi khong
-- doc ra duoc ban bam hop le nao, nen sinh vien nao con mang gia tri do se khong the dang nhap.
-- Do la cach dung de mot lan doi luoc do lam do dang bi that bai: dong lai, chu khong mo ra.
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
    -- Mon bat buoc tinh vao dieu kien tot nghiep; mon tu chon thi sinh vien tu chon.
    is_required  BOOLEAN NOT NULL DEFAULT TRUE
);

-- Which courses must be passed before another course may be taken.
-- Nhung mon phai dat truoc khi duoc hoc mot mon khac.
--
-- This table, not the model, decides whether a prerequisite is met. A student can insist
-- they have already taken the prerequisite and the model may well believe them; the join
-- against `grades` does not.
-- Chinh bang nay quyet dinh mon tien quyet da dat hay chua, khong phai model. Sinh vien co
-- the mot muc khang dinh minh da hoc mon tien quyet va model hoan toan co the tin theo;
-- con phep join voi bang `grades` thi khong.
CREATE TABLE IF NOT EXISTS prerequisites (
    course_code  TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    prereq_code  TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    PRIMARY KEY (course_code, prereq_code),
    -- A course cannot require itself, which would make it impossible to ever register for.
    -- Mot mon khong the tu lam tien quyet cua chinh no, neu khong se khong bao gio dang ky duoc.
    CHECK (course_code <> prereq_code)
);

-- A class a student can actually register for: one course, one timetable slot, one room.
-- Mot lop hoc phan sinh vien co the dang ky: mot mon, mot khung gio, mot phong.
--
-- `enrolled` is a counter kept next to `capacity` so the two can be compared under a single
-- row lock at registration time. The CHECK below is the last line of defence: even if the
-- application logic were wrong, PostgreSQL would still refuse to overfill the class.
-- `enrolled` la bo dem dat canh `capacity` de hai gia tri nay duoc so sanh duoi cung mot
-- khoa dong luc dang ky. Rang buoc CHECK ben duoi la lop phong thu cuoi cung: du logic ung
-- dung co sai, PostgreSQL van khong cho lop vuot si so.
CREATE TABLE IF NOT EXISTS class_sections (
    id            SERIAL PRIMARY KEY,
    course_code   TEXT NOT NULL REFERENCES courses(course_code) ON DELETE CASCADE,
    section_no    TEXT NOT NULL,
    semester      TEXT NOT NULL,
    lecturer      TEXT NOT NULL,
    capacity      INTEGER NOT NULL CHECK (capacity > 0),
    enrolled      INTEGER NOT NULL DEFAULT 0,
    -- 2 = Monday ... 8 = Sunday, following how Vietnamese timetables are written.
    -- 2 = thu Hai ... 8 = Chu nhat, theo cach thoi khoa bieu Viet Nam van ghi.
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
-- Diem da co. Cot `passed` duoc luu san thay vi tinh lai tu `score`, de quy tac dat mon chi
-- nam o mot cho va viec kiem tra tien quyet chi con la mot phep tra cuu don gian.
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
-- Mot lenh dang ky da thuc su thanh cong.
--
-- course_code is denormalised from class_sections purely so the second UNIQUE below can
-- exist: registering for two different sections of the same course in one semester is a
-- mistake the database should refuse, not something the application has to remember to check.
-- course_code duoc lap lai tu class_sections chi de rang buoc UNIQUE thu hai ben duoi ton tai
-- duoc: dang ky hai lop khac nhau cua cung mot mon trong mot ky la loi ma database nen tu tu
-- choi, chu khong phai thu ma ung dung phai nho de kiem tra.
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
-- Khoang thoi gian sinh vien duoc phep dang ky hoc phan.
CREATE TABLE IF NOT EXISTS registration_windows (
    semester   TEXT PRIMARY KEY,
    opens_at   TIMESTAMPTZ NOT NULL,
    closes_at  TIMESTAMPTZ NOT NULL,
    CHECK (closes_at > opens_at)
);

-- Conversation memory. Scoped by student as well as by session, so guessing another
-- person's session id does not reveal their conversation.
-- Bo nho hoi thoai. Duoc gioi han theo ca sinh vien lan phien, nen doan trung session id
-- cua nguoi khac cung khong doc duoc hoi thoai cua ho.
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
-- Mot lenh dang ky tro ly da chuan bi nhung chua thuc hien.
--
-- This table is what makes "the student confirmed" a fact instead of a claim. The model
-- cannot set a flag to assert consent: it can only create a row here, read the class back
-- to the student, and wait. The row records which turn created it, and the guardrail
-- refuses to execute a row that was created in the turn currently running - so consent
-- always costs the student a separate message that the model cannot fabricate.
-- Bang nay bien "sinh vien da xac nhan" tu mot loi khai thanh mot su that. Model khong the
-- dat mot co de tu khang dinh la sinh vien da dong y: no chi co the tao mot dong o day, doc
-- lai thong tin lop cho sinh vien nghe, roi cho. Dong nay ghi lai luot nao da tao ra no, va
-- guardrail tu choi thuc thi mot dong duoc tao ra trong chinh luot dang chay - nen su dong y
-- luon phai tra bang mot tin nhan rieng cua sinh vien, thu ma model khong bia ra duoc.
CREATE TABLE IF NOT EXISTS pending_registrations (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    student_id        TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
    -- The turn that created this row. Confirming inside the same turn is refused.
    -- Luot da tao ra dong nay. Xac nhan ngay trong cung luot do se bi tu choi.
    created_turn_id   TEXT NOT NULL,
    class_section_id  INTEGER NOT NULL REFERENCES class_sections(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'cho_xac_nhan'
                      CHECK (status IN ('cho_xac_nhan', 'da_thuc_hien')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_registrations(session_id, student_id);

-- Audit trail of every tool call the agent makes. The school must be able to answer
-- "what did the agent actually do", so this is written before the result is returned.
-- Nhat ky moi lan agent goi tool. Nha truong phai tra loi duoc "agent da lam gi",
-- nen ban ghi nay duoc luu truoc khi tra ket qua ve.
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
