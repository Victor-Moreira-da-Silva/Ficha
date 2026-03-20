import os
import re
import shutil
import sqlite3
from functools import wraps
from pathlib import Path
from uuid import uuid4

from flask import (
    Flask,
    flash,
    g,
    has_app_context,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

try:
    import pytesseract
    import pypdfium2 as pdfium
except Exception:  # pragma: no cover - dependência opcional em tempo de execução
    pytesseract = None
    pdfium = None

try:
    import oracledb
except Exception:  # pragma: no cover - dependência opcional em tempo de execução
    oracledb = None

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "ficha.db"
UPLOAD_ROOT = BASE_DIR / "storage" / "atendimentos"
ALLOWED_EXTENSIONS = {"pdf"}
WINDOWS_TESSERACT_LOCATIONS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
DEFAULT_OCR_CAPTURE_REGION = {
    "x": 0.55,
    "y": 0.08,
    "width": 0.43,
    "height": 0.30,
}
ORACLE_CLIENT_DIR = os.getenv("ORACLE_CLIENT_DIR", r"C:\Oracle\instantclient\instantclient_23_0")
ORACLE_USER = os.getenv("ORACLE_USER", "soleitura")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "soleitura")
ORACLE_DSN = os.getenv(
    "ORACLE_DSN",
    "dbprd.7141.cloudmv.com.br:1521/PRD7141.db7141.mv7141vcn.oraclevcn.com",
)
ORACLE_CLIENT_INITIALIZED = False

ATTENDANCE_PATTERNS = [
    re.compile(r"(?:n[ºo°]?\s*(?:do\s*)?)?atendimento\D{0,15}(\d{4,})", re.IGNORECASE),
    re.compile(r"atend\D{0,15}(\d{4,})", re.IGNORECASE),
    re.compile(r"\b(\d{6,})\b"),
]

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
app.config["UPLOAD_ROOT"] = str(UPLOAD_ROOT)
app.config["DATABASE"] = str(DATABASE)

PRESCRIPTION_QUERY = """
SELECT
    SP.CD_SOLSAI_PRO,
    SP.CD_PRE_MED,
    SP.CD_ATENDIMENTO,
    TO_CHAR(PMED.DT_PRE_MED,'DD/MM/YYYY') || ' ' || TO_CHAR(PMED.HR_PRE_MED,'HH24:MI') AS DATA_HORA,
    PR.DS_PRODUTO,
    IP.QT_SOLICITADO,
    TA.CD_COR_REFERENCIA,
    PA.NM_PACIENTE,
    TRUNC(MONTHS_BETWEEN(SYSDATE, PA.DT_NASCIMENTO)/12) AS IDADE,
    (
        SELECT MAX(TRIM(PM2.CD_FOR_APL))
        FROM DBAMV.ITPRE_MED PM2
        WHERE PM2.CD_PRE_MED = SP.CD_PRE_MED
          AND PM2.CD_PRODUTO = IP.CD_PRODUTO
          AND PM2.CD_FOR_APL IS NOT NULL
    ) CD_FOR_APL
FROM DBAMV.SOLSAI_PRO SP
JOIN DBAMV.ITSOLSAI_PRO IP
    ON IP.CD_SOLSAI_PRO = SP.CD_SOLSAI_PRO
LEFT JOIN DBAMV.PRE_MED PMED
    ON PMED.CD_PRE_MED = SP.CD_PRE_MED
LEFT JOIN DBAMV.PRODUTO PR
    ON PR.CD_PRODUTO = IP.CD_PRODUTO
LEFT JOIN DBAMV.ATENDIME A
    ON A.CD_ATENDIMENTO = SP.CD_ATENDIMENTO
LEFT JOIN DBAMV.PACIENTE PA
    ON PA.CD_PACIENTE = A.CD_PACIENTE
LEFT JOIN DBAMV.TRIAGEM_ATENDIMENTO TA
    ON TA.CD_ATENDIMENTO = A.CD_ATENDIMENTO
WHERE
    TO_CHAR(SP.CD_ATENDIMENTO) LIKE '%' || :attendance_number_str || '%'
ORDER BY PMED.DT_PRE_MED DESC
"""

ATTENDANCE_CONTEXT_QUERY = """
WITH base AS (
    SELECT
        a.cd_paciente,
        p.nm_paciente,
        a.cd_atendimento,
        a.dt_atendimento,
        t.ds_queixa_principal,
        t.cd_cor_referencia,
        sv.cd_sinal_vital,
        ic.valor,
        csv.data_coleta,
        ROW_NUMBER() OVER (
            PARTITION BY a.cd_atendimento, sv.cd_sinal_vital
            ORDER BY csv.data_coleta DESC
        ) AS rn
    FROM dbamv.atendime a
    JOIN dbamv.paciente p
      ON p.cd_paciente = a.cd_paciente
    LEFT JOIN dbamv.triagem_atendimento t
      ON t.cd_atendimento = a.cd_atendimento
    LEFT JOIN dbamv.coleta_sinal_vital csv
      ON csv.cd_atendimento = a.cd_atendimento
    LEFT JOIN dbamv.itcoleta_sinal_vital ic
      ON ic.cd_coleta_sinal_vital = csv.cd_coleta_sinal_vital
    LEFT JOIN dbamv.sinal_vital sv
      ON sv.cd_sinal_vital = ic.cd_sinal_vital
    WHERE a.tp_atendimento = 'U'
),
atendimentos AS (
    SELECT
        cd_paciente,
        nm_paciente,
        cd_atendimento,
        dt_atendimento,
        LAG(dt_atendimento) OVER (
            PARTITION BY cd_paciente
            ORDER BY dt_atendimento
        ) AS dt_anterior
    FROM (
        SELECT
            cd_paciente,
            nm_paciente,
            cd_atendimento,
            dt_atendimento
        FROM base
        GROUP BY
            cd_paciente,
            nm_paciente,
            cd_atendimento,
            dt_atendimento
    )
),
retornos AS (
    SELECT DISTINCT cd_paciente
    FROM atendimentos
    WHERE dt_anterior IS NOT NULL
      AND TRUNC(dt_atendimento) - TRUNC(dt_anterior) <= 7
)
SELECT
    b.cd_paciente,
    b.nm_paciente,
    TRUNC(b.dt_atendimento) AS dt_atendimento,
    MAX(b.ds_queixa_principal) AS queixa_principal,
    MAX(CASE
        WHEN b.cd_cor_referencia = 1 THEN 'BRANCO'
        WHEN b.cd_cor_referencia = 2 THEN 'VERMELHO'
        WHEN b.cd_cor_referencia = 3 THEN 'AMARELO'
        WHEN b.cd_cor_referencia = 4 THEN 'VERDE'
        WHEN b.cd_cor_referencia = 5 THEN 'AZUL'
        ELSE 'NAO CLASSIFICADO'
    END) AS classificacao_risco,
    MAX(CASE WHEN b.cd_sinal_vital = 1  AND b.rn = 1 THEN b.valor END) AS temperatura,
    MAX(CASE WHEN b.cd_sinal_vital = 2  AND b.rn = 1 THEN b.valor END) AS frequencia_cardiaca,
    MAX(CASE WHEN b.cd_sinal_vital = 4  AND b.rn = 1 THEN b.valor END) AS pressao_sistolica,
    MAX(CASE WHEN b.cd_sinal_vital = 5  AND b.rn = 1 THEN b.valor END) AS pas_pad,
    MAX(CASE WHEN b.cd_sinal_vital = 11 AND b.rn = 1 THEN b.valor END) AS spo2,
    MAX(CASE WHEN b.cd_sinal_vital = 13 AND b.rn = 1 THEN b.valor END) AS glicemia,
    prd.ds_produto AS medicamento
FROM base b
LEFT JOIN retornos r
  ON r.cd_paciente = b.cd_paciente
LEFT JOIN dbamv.pre_med pm
  ON pm.cd_atendimento = b.cd_atendimento
LEFT JOIN dbamv.itpre_med ipm
  ON ipm.cd_pre_med = pm.cd_pre_med
  AND NVL(ipm.sn_cancelado, 'N') = 'N'
LEFT JOIN dbamv.produto prd
  ON prd.cd_produto = ipm.cd_produto
WHERE
    TO_CHAR(b.cd_atendimento) LIKE '%' || :attendance_number_str || '%'
GROUP BY
    b.cd_paciente,
    b.nm_paciente,
    TRUNC(b.dt_atendimento),
    b.cd_atendimento,
    prd.ds_produto
ORDER BY
    NLSSORT(b.nm_paciente, 'NLS_SORT=BINARY_AI'),
    TRUNC(b.dt_atendimento),
    prd.ds_produto
"""

def resolve_tesseract_cmd() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    candidates = []

    if configured:
        configured_path = Path(configured)
        if configured_path.is_dir():
            candidates.append(configured_path / "tesseract.exe")
            candidates.append(configured_path / "tesseract")
        else:
            candidates.append(configured_path)

    which_result = shutil.which("tesseract")
    if which_result:
        candidates.append(Path(which_result))

    for location in WINDOWS_TESSERACT_LOCATIONS:
        candidates.append(Path(location))

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return None


# --- Banco de dados -------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config["DATABASE"])
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('recepcao', 'faturamento', 'admin')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            attendance_number TEXT NOT NULL,
            uploaded_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(uploaded_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS ocr_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            width_ratio REAL NOT NULL,
            height_ratio REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    admin_exists = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
    if admin_exists is None:
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("admin", generate_password_hash("admin123"), "admin"),
        )
    ocr_settings = db.execute("SELECT id FROM ocr_settings WHERE id = 1").fetchone()
    if ocr_settings is None:
        db.execute(
            """
            INSERT INTO ocr_settings (id, x_ratio, y_ratio, width_ratio, height_ratio)
            VALUES (1, ?, ?, ?, ?)
            """,
            (
                DEFAULT_OCR_CAPTURE_REGION["x"],
                DEFAULT_OCR_CAPTURE_REGION["y"],
                DEFAULT_OCR_CAPTURE_REGION["width"],
                DEFAULT_OCR_CAPTURE_REGION["height"],
            ),
        )
    db.commit()
    db.close()


# --- Autenticação ---------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if "user_id" not in session:
            flash("Faça login para acessar o sistema.", "warning")
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view



def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped_view(**kwargs):
            user = current_user()
            if user is None:
                flash("Faça login para continuar.", "warning")
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("Você não tem permissão para acessar esta área.", "danger")
                return redirect(url_for("dashboard"))
            return view(**kwargs)

        return wrapped_view

    return decorator



def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        "SELECT id, username, role, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# --- OCR e arquivos -------------------------------------------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS



def get_ocr_capture_region() -> dict[str, float]:
    default_region = DEFAULT_OCR_CAPTURE_REGION.copy()

    if has_app_context():
        row = get_db().execute(
            """
            SELECT x_ratio, y_ratio, width_ratio, height_ratio
            FROM ocr_settings
            WHERE id = 1
            """
        ).fetchone()
        if row:
            return {
                "x": row["x_ratio"],
                "y": row["y_ratio"],
                "width": row["width_ratio"],
                "height": row["height_ratio"],
            }
        return default_region

    db_path = Path(app.config.get("DATABASE", DATABASE))
    if not db_path.exists():
        return default_region

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute(
            """
            SELECT x_ratio, y_ratio, width_ratio, height_ratio
            FROM ocr_settings
            WHERE id = 1
            """
        ).fetchone()
    finally:
        db.close()

    if row:
        return {
            "x": row["x_ratio"],
            "y": row["y_ratio"],
            "width": row["width_ratio"],
            "height": row["height_ratio"],
        }
    return default_region


def get_focused_pdf_bounds(width: float, height: float) -> tuple[float, float, float, float]:
    region = get_ocr_capture_region()
    return (
        width * region["x"],
        height * (1 - (region["y"] + region["height"])),
        width * (region["x"] + region["width"]),
        height * (1 - region["y"]),
    )


def get_focused_image_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    region = get_ocr_capture_region()
    return (
        int(width * region["x"]),
        int(height * region["y"]),
        int(width * (region["x"] + region["width"])),
        int(height * (region["y"] + region["height"])),
    )


def extract_text_layer_from_pdf(pdf_path: Path, focused: bool = False) -> str:
    if pdfium is None:
        return ""

    pdf = pdfium.PdfDocument(str(pdf_path))
    texts = []
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            text_page = page.get_textpage()
            try:
                if focused:
                    width, height = page.get_size()
                    left, bottom, right, top = get_focused_pdf_bounds(width, height)
                    text = text_page.get_text_bounded(
                        left=left,
                        bottom=bottom,
                        right=right,
                        top=top,
                    )
                else:
                    text = text_page.get_text_range()
            finally:
                text_page.close()
            if text:
                texts.append(text)
    finally:
        pdf.close()
    return "\n".join(texts)



def extract_text_with_ocr(pdf_path: Path, focused: bool = False) -> str:
    if pytesseract is None or pdfium is None:
        raise RuntimeError(
            "OCR indisponível porque as dependências Python do OCR não estão instaladas."
        )

    tesseract_cmd = resolve_tesseract_cmd()
    if not tesseract_cmd:
        raise RuntimeError(
            "OCR indisponível: o Tesseract não está instalado ou não está no PATH. "
            "Se o seu PDF já tiver texto, tente exportá-lo novamente como PDF pesquisável. "
            "Se for um PDF escaneado, instale o Tesseract e reinicie o sistema. "
            "No Windows, o sistema também procura automaticamente em C:\\Program Files\\Tesseract-OCR\\tesseract.exe."
        )

    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    pdf = pdfium.PdfDocument(str(pdf_path))
    texts = []
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            bitmap = page.render(scale=2)
            image = bitmap.to_pil()
            if focused:
                left, top, right, bottom = get_focused_image_bounds(*image.size)
                image = image.crop((left, top, right, bottom))
            texts.append(pytesseract.image_to_string(image, lang="por+eng"))
    finally:
        pdf.close()
    return "\n".join(texts)



def find_attendance_number(text: str) -> str | None:
    normalized = " ".join(text.split())
    for pattern in ATTENDANCE_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return match.group(1)
    return None



def extract_attendance_number(pdf_path: Path) -> str:
    extraction_steps = (
        (extract_text_layer_from_pdf, True),
        (extract_text_layer_from_pdf, False),
        (extract_text_with_ocr, True),
        (extract_text_with_ocr, False),
    )

    for extractor, focused in extraction_steps:
        extracted_text = extractor(pdf_path, focused=focused)
        attendance_number = find_attendance_number(extracted_text)
        if attendance_number:
            return attendance_number

    raise ValueError(
        "Não foi possível identificar o número do atendimento no PDF enviado."
    )


def build_storage_path(attendance_number: str) -> Path:
    target_dir = Path(app.config["UPLOAD_ROOT"]) / attendance_number
    target_dir.mkdir(parents=True, exist_ok=True)

    candidate = target_dir / f"{attendance_number}.pdf"
    counter = 2
    while candidate.exists():
        candidate = target_dir / f"{attendance_number}-{counter}.pdf"
        counter += 1

    return candidate


def initialize_oracle_client():
    global ORACLE_CLIENT_INITIALIZED
    if ORACLE_CLIENT_INITIALIZED or oracledb is None:
        return

    client_dir = Path(ORACLE_CLIENT_DIR)
    if client_dir.exists():
        try:
            oracledb.init_oracle_client(lib_dir=str(client_dir))
        except Exception:
            pass
    ORACLE_CLIENT_INITIALIZED = True


def build_oracle_params(attendance_number: str) -> dict:
    normalized_str = attendance_number.strip()

    # remove zeros à esquerda
    normalized_str = normalized_str.lstrip("0")

    print("DEBUG atendimento:", repr(normalized_str))

    return {
        "attendance_number_str": normalized_str
    }


def run_oracle_query(query: str, attendance_number: str) -> list[dict]:
    if oracledb is None:
        raise RuntimeError("Biblioteca Oracle indisponível.")

    initialize_oracle_client()

    params = build_oracle_params(attendance_number)

    print("\n===== EXECUTANDO QUERY =====")
    print("ATENDIMENTO:", attendance_number)
    print("PARAMS:", params)

    connection = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )

    try:
        cursor = connection.cursor()
        cursor.execute(query, params)

        rows = cursor.fetchall()

        print("TOTAL DE LINHAS:", len(rows))

        columns = [column[0].lower() for column in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    except Exception as e:
        print("🔥 ERRO ORACLE:", str(e))  # 👈 AGORA VAI APARECER
        raise

    finally:
        connection.close()


def fetch_attendance_context(attendance_number: str) -> dict:
    try:
        print("DEBUG atendimento:", attendance_number)
        prescriptions = run_oracle_query(PRESCRIPTION_QUERY, attendance_number)
        attendance_rows = run_oracle_query(ATTENDANCE_CONTEXT_QUERY, attendance_number)
        return {
            "prescriptions": prescriptions,
            "attendance_rows": attendance_rows,
            "summary": attendance_rows[0] if attendance_rows else None,
            "error": None,
        }
    except Exception as exc:
        return {
            "prescriptions": [],
            "attendance_rows": [],
            "summary": None,
            "error": str(exc),
        }


# --- Rotas ----------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,)
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Bem-vindo(a), {user['username']}!", "success")
            return redirect(url_for("dashboard"))

        flash("Usuário ou senha inválidos.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Sessão encerrada com sucesso.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()
    uploads = db.execute(
        """
        SELECT uploads.id, uploads.original_filename, uploads.attendance_number,
               uploads.stored_path, uploads.created_at, users.username AS uploader
        FROM uploads
        JOIN users ON users.id = uploads.uploaded_by
        ORDER BY uploads.created_at DESC
        LIMIT 20
        """
    ).fetchall()
    user_count = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
    upload_count = db.execute("SELECT COUNT(*) AS total FROM uploads").fetchone()["total"]
    return render_template(
        "dashboard.html",
        uploads=uploads,
        user=user,
        user_count=user_count,
        upload_count=upload_count,
    )


@app.route("/upload", methods=["GET", "POST"])
@login_required
@role_required("recepcao", "admin")
def upload_pdf():
    if request.method == "POST":
        if "pdf_file" not in request.files:
            flash("Selecione um arquivo PDF.", "danger")
            return redirect(url_for("upload_pdf"))

        file = request.files["pdf_file"]
        if file.filename == "":
            flash("Selecione um arquivo PDF.", "danger")
            return redirect(url_for("upload_pdf"))

        if not allowed_file(file.filename):
            flash("Apenas arquivos PDF são aceitos.", "danger")
            return redirect(url_for("upload_pdf"))

        temp_dir = Path(app.config["UPLOAD_ROOT"]) / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        safe_name = secure_filename(file.filename)
        temp_path = temp_dir / f"{uuid4()}-{safe_name}"
        file.save(temp_path)

        try:
            attendance_number = extract_attendance_number(temp_path)
            final_path = build_storage_path(attendance_number)
            temp_path.replace(final_path)
            get_db().execute(
                """
                INSERT INTO uploads (original_filename, stored_path, attendance_number, uploaded_by)
                VALUES (?, ?, ?, ?)
                """,
                (
                    safe_name,
                    str(final_path.relative_to(BASE_DIR)),
                    attendance_number,
                    current_user()["id"],
                ),
            )
            get_db().commit()
            flash(
                f"PDF processado com sucesso. Atendimento identificado: {attendance_number}.",
                "success",
            )
            return redirect(url_for("dashboard"))
        except Exception as exc:
            if temp_path.exists():
                temp_path.unlink()
            flash(str(exc), "danger")
    return render_template("upload.html")


@app.route("/attendance/<attendance_number>")
@login_required
@role_required("faturamento", "admin")
def attendance_details(attendance_number: str):
    context = fetch_attendance_context(attendance_number)
    return render_template(
        "attendance_details.html",
        attendance_number=attendance_number,
        prescriptions=context["prescriptions"],
        attendance_rows=context["attendance_rows"],
        summary=context["summary"],
        oracle_error=context["error"],
    )


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
@role_required("admin")
def manage_users():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            username = request.form.get("username", "").strip()
            role = request.form.get("role", "").strip()
            password = request.form.get("password", "")

            if not username or not password or role not in {"recepcao", "faturamento", "admin"}:
                flash("Preencha usuário, senha e perfil corretamente.", "danger")
            elif len(username) < 3 or len(password) < 4:
                flash("Usuário deve ter ao menos 3 caracteres e a senha 4.", "danger")
            else:
                try:
                    db.execute(
                        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                        (username, generate_password_hash(password), role),
                    )
                    db.commit()
                    flash("Usuário criado com sucesso.", "success")
                except sqlite3.IntegrityError:
                    flash("Já existe um usuário com esse nome.", "danger")

        elif action == "change_password":
            user_id = request.form.get("user_id", type=int)
            new_password = request.form.get("new_password", "")
            if not user_id or not new_password:
                flash("Informe o usuário e a nova senha.", "danger")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), user_id),
                )
                db.commit()
                flash("Senha atualizada com sucesso.", "success")

        anchor = "cadastro-usuario" if action == "create" else "alterar-senha"
        return redirect(f"{url_for('manage_users')}#{anchor}")

    users = db.execute(
        "SELECT id, username, role, created_at FROM users ORDER BY username ASC"
    ).fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/ocr-settings", methods=["GET", "POST"])
@login_required
@role_required("admin")
def manage_ocr_settings():
    db = get_db()

    if request.method == "POST":
        x_percent = request.form.get("x_percent", type=float)
        y_percent = request.form.get("y_percent", type=float)
        width_percent = request.form.get("width_percent", type=float)
        height_percent = request.form.get("height_percent", type=float)

        values = [x_percent, y_percent, width_percent, height_percent]
        if any(value is None for value in values):
            flash("Preencha todos os campos da área do OCR.", "danger")
            return redirect(url_for("manage_ocr_settings"))

        if (
            x_percent < 0
            or y_percent < 0
            or width_percent <= 0
            or height_percent <= 0
            or x_percent + width_percent > 100
            or y_percent + height_percent > 100
        ):
            flash("A área do OCR deve ficar dentro de 0% a 100% da página.", "danger")
            return redirect(url_for("manage_ocr_settings"))

        db.execute(
            """
            UPDATE ocr_settings
            SET x_ratio = ?, y_ratio = ?, width_ratio = ?, height_ratio = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                x_percent / 100,
                y_percent / 100,
                width_percent / 100,
                height_percent / 100,
            ),
        )
        db.commit()
        flash("Área do OCR atualizada com sucesso.", "success")
        return redirect(url_for("manage_ocr_settings"))

    region = get_ocr_capture_region()
    return render_template(
        "ocr_settings.html",
        region={
            "x_percent": round(region["x"] * 100, 2),
            "y_percent": round(region["y"] * 100, 2),
            "width_percent": round(region["width"] * 100, 2),
            "height_percent": round(region["height"] * 100, 2),
        },
    )


@app.route("/files/<path:relative_path>")
@login_required
@role_required("faturamento", "admin")
def serve_file(relative_path: str):
    file_path = BASE_DIR / relative_path
    return send_from_directory(file_path.parent, file_path.name, as_attachment=True)


@app.route("/uploads/<int:upload_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_upload(upload_id: int):
    db = get_db()
    upload = db.execute(
        "SELECT id, stored_path, attendance_number FROM uploads WHERE id = ?",
        (upload_id,),
    ).fetchone()

    if upload is None:
        flash("Arquivo não encontrado.", "danger")
        return redirect(url_for("dashboard"))

    file_path = BASE_DIR / upload["stored_path"]
    if file_path.exists():
        file_path.unlink()
        parent_dir = file_path.parent
        if parent_dir.exists() and not any(parent_dir.iterdir()):
            parent_dir.rmdir()

    db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    db.commit()
    flash("Arquivo excluído com sucesso.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
