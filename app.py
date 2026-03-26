import os
import re
import shutil
import sqlite3
import base64
from functools import wraps
from datetime import datetime
from io import BytesIO
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
from PIL import Image
from PIL import Image, ImageDraw, ImageFont
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
    PREST.NM_PRESTADOR AS MEDICO_SOLICITANTE,
    A.CD_CID,
    CID.DS_CID,
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
LEFT JOIN DBAMV.PRESTADOR PREST
    ON PREST.CD_PRESTADOR = PMED.CD_PRESTADOR
LEFT JOIN DBAMV.CID CID
    ON CID.CD_CID = A.CD_CID
WHERE
    SP.CD_ATENDIMENTO = :attendance_number_num
ORDER BY PMED.DT_PRE_MED DESC
"""

REPORT_DAILY_QUERY = """
SELECT
    TRUNC(A.DT_ATENDIMENTO) AS DATA,
    COUNT(*) AS TOTAL
FROM DBAMV.ATENDIME A
WHERE A.TP_ATENDIMENTO = 'U'
GROUP BY TRUNC(A.DT_ATENDIMENTO)
ORDER BY DATA
"""

REPORT_MONTHLY_QUERY = """
SELECT
    TO_CHAR(A.DT_ATENDIMENTO, 'YYYY-MM') AS MES,
    COUNT(*) AS TOTAL
FROM DBAMV.ATENDIME A
WHERE 
    A.TP_ATENDIMENTO = 'U'
    AND A.DT_ATENDIMENTO >= ADD_MONTHS(TRUNC(SYSDATE, 'MM'), -11)
GROUP BY TO_CHAR(A.DT_ATENDIMENTO, 'YYYY-MM')
ORDER BY MES
"""

REPORT_YEARLY_QUERY = """
SELECT
    TO_CHAR(A.DT_ATENDIMENTO, 'YYYY') AS ANO,
    COUNT(*) AS TOTAL
FROM DBAMV.ATENDIME A
WHERE A.TP_ATENDIMENTO = 'U'
GROUP BY TO_CHAR(A.DT_ATENDIMENTO, 'YYYY')
ORDER BY ANO
"""

LAB_EXAMS_QUERY = """
SELECT
    PL.CD_ATENDIMENTO,
    TO_CHAR(PL.DT_PEDIDO,'DD/MM/YYYY HH24:MI') AS DATA_HORA,
    EXL.NM_EXA_LAB AS EXAME,
    PA.NM_PACIENTE,
    PREST.NM_PRESTADOR AS MEDICO_SOLICITANTE,
    A.CD_CID,
    CID.DS_CID
FROM DBAMV.PED_LAB PL
LEFT JOIN DBAMV.ITPED_LAB IPL
    ON IPL.CD_PED_LAB = PL.CD_PED_LAB
LEFT JOIN DBAMV.EXA_LAB EXL
    ON EXL.CD_EXA_LAB = IPL.CD_EXA_LAB
LEFT JOIN DBAMV.ATENDIME A
    ON A.CD_ATENDIMENTO = PL.CD_ATENDIMENTO
LEFT JOIN DBAMV.PACIENTE PA
    ON PA.CD_PACIENTE = A.CD_PACIENTE
LEFT JOIN DBAMV.PRESTADOR PREST
    ON PREST.CD_PRESTADOR = PL.CD_PRESTADOR
LEFT JOIN DBAMV.CID CID
    ON CID.CD_CID = A.CD_CID
WHERE
    PL.CD_ATENDIMENTO = :attendance_number_num
ORDER BY PL.DT_PEDIDO DESC
"""

IMAGING_EXAMS_QUERY = """
SELECT
    PRX.CD_ATENDIMENTO,
    TO_CHAR(PRX.DT_PEDIDO,'DD/MM/YYYY') || ' ' || TO_CHAR(PRX.HR_PEDIDO,'HH24:MI') AS DATA_HORA,
    EXR.DS_EXA_RX AS EXAME,
    PA.NM_PACIENTE,
    PREST.NM_PRESTADOR AS MEDICO_SOLICITANTE,
    A.CD_CID,
    CID.DS_CID
FROM DBAMV.PED_RX PRX
LEFT JOIN DBAMV.ITPED_RX IPRX
    ON IPRX.CD_PED_RX = PRX.CD_PED_RX
LEFT JOIN DBAMV.EXA_RX EXR
    ON EXR.CD_EXA_RX = IPRX.CD_EXA_RX
LEFT JOIN DBAMV.ATENDIME A
    ON A.CD_ATENDIMENTO = PRX.CD_ATENDIMENTO
LEFT JOIN DBAMV.PACIENTE PA
    ON PA.CD_PACIENTE = A.CD_PACIENTE
LEFT JOIN DBAMV.PRESTADOR PREST
    ON PREST.CD_PRESTADOR = PRX.CD_PRESTADOR
LEFT JOIN DBAMV.CID CID
    ON CID.CD_CID = A.CD_CID
WHERE
    TO_CHAR(PRX.CD_ATENDIMENTO) LIKE '%' || :attendance_number_str || '%'
ORDER BY PRX.DT_PEDIDO DESC, PRX.HR_PEDIDO DESC
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
    MAX(a.cd_cid) AS cd_cid,
    MAX(cid.ds_cid) AS ds_cid,
    MAX(prest.nm_prestador) AS medico_solicitante,
    prd.ds_produto AS medicamento
FROM base b
LEFT JOIN retornos r
  ON r.cd_paciente = b.cd_paciente
LEFT JOIN dbamv.atendime a
  ON a.cd_atendimento = b.cd_atendimento
LEFT JOIN dbamv.cid cid
  ON cid.cd_cid = a.cd_cid
LEFT JOIN dbamv.pre_med pm
  ON pm.cd_atendimento = b.cd_atendimento
LEFT JOIN dbamv.prestador prest
  ON prest.cd_prestador = pm.cd_prestador
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

ONLINE_SIGNATURE_QUERY = """
SELECT
    a.CD_ATENDIMENTO,
    a.DT_ATENDIMENTO,
    a.HR_ATENDIMENTO,
    p.NM_PACIENTE,
    p.DT_NASCIMENTO,
    p.TP_SEXO,
    p.NR_CPF,
    p.NR_CNS,
    p.DS_ENDERECO,
    p.NR_ENDERECO,
    p.NM_BAIRRO,
    p.NR_CEP,
    p.NR_SAME,
    p.NR_FONE,
    p.NM_MAE,
    p.NM_PAI
FROM ATENDIME a, PACIENTE p
WHERE a.CD_PACIENTE = p.CD_PACIENTE
AND a.CD_ATENDIMENTO = :attendance_number
"""

SIGNATURE_BOX_DEFAULT = {
    "page_index": 0,
    "x_ratio": 0.06,
    "y_ratio": 0.54,
    "width_ratio": 0.56,
    "height_ratio": 0.05,
}
PDF_EXPORT_DPI = 96
SIGNATURE_BASE_PDF_DPI = 150
SIGNED_PDF_RENDER_SCALE = 4
BIND_PARAM_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")

def format_patient_datetime(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y")
    text_value = str(value).strip()
    if not text_value:
        return ""
    return text_value


def get_patient_value(patient_data, key: str, fallback: str = "-") -> str:
    if not patient_data:
        return fallback
    value = None
    if hasattr(patient_data, "get"):
        value = patient_data.get(key)
        if value is None:
            value = patient_data.get(key.upper())
        if value is None:
            value = patient_data.get(key.lower())
    else:
        try:
            value = patient_data[key]
        except Exception:
            try:
                value = patient_data[key.upper()]
            except Exception:
                try:
                    value = patient_data[key.lower()]
                except Exception:
                    value = None

    if value in (None, ""):
        return fallback
    return str(value)


def generate_signature_base_pdf(attendance_number: str, patient_data, target_pdf_path: Path) -> None:
    image_width, image_height = 1240, 1754
    image = Image.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)
    base_font = ImageFont.load_default()

    def line(y_pos, thickness=2):
        draw.rectangle((40, y_pos, image_width - 40, y_pos + thickness), fill="black")

    draw.text((40, 30), "FILIAL - PRONTO SOCORRO MUN PORTO FELIZ", fill="black", font=base_font)
    draw.text((40, 54), "GOVERNADOR MARIO COVAS - PORTO FELIZ - SP", fill="black", font=base_font)
    draw.text((560, 120), "FICHA DE ATENDIMENTO", fill="black", font=base_font)
    draw.text((920, 60), "|||| ||| |||| |||| |||", fill="black", font=base_font)
    draw.text((900, 84), attendance_number, fill="black", font=base_font)

    line(180, 8)
    line(238, 8)
    line(520, 8)

    draw.text((40, 200), f"ATENDIMENTO: {attendance_number}", fill="black", font=base_font)
    draw.text((380, 200), f"DATA DA ENTRADA: {format_patient_datetime(get_patient_value(patient_data, 'dt_atendimento', ''))}", fill="black", font=base_font)
    draw.text((760, 200), f"HORA: {get_patient_value(patient_data, 'hr_atendimento', '-')}", fill="black", font=base_font)

    draw.text((40, 270), f"PRONTUÁRIO: {get_patient_value(patient_data, 'nr_same', '-')}", fill="black", font=base_font)
    draw.text((520, 270), f"CNS: {get_patient_value(patient_data, 'nr_cns', '-')}", fill="black", font=base_font)
    draw.text((40, 300), f"PACIENTE: {get_patient_value(patient_data, 'nm_paciente', '-')}", fill="black", font=base_font)
    draw.text((40, 330), f"SEXO: {get_patient_value(patient_data, 'tp_sexo', '-')}", fill="black", font=base_font)
    draw.text((520, 330), f"DATA DE NASCIMENTO: {format_patient_datetime(get_patient_value(patient_data, 'dt_nascimento', ''))}", fill="black", font=base_font)
    draw.text((920, 330), "IDADE: -", fill="black", font=base_font)
    draw.text((40, 360), f"CPF: {get_patient_value(patient_data, 'nr_cpf', '-')}", fill="black", font=base_font)
    draw.text((40, 390), f"ENDEREÇO: {get_patient_value(patient_data, 'ds_endereco', '-')} {get_patient_value(patient_data, 'nr_endereco', '')}", fill="black", font=base_font)
    draw.text((40, 420), f"CIDADE/BAIRRO: {get_patient_value(patient_data, 'nm_bairro', '-')}", fill="black", font=base_font)
    draw.text((850, 420), f"CEP: {get_patient_value(patient_data, 'nr_cep', '-')}", fill="black", font=base_font)
    draw.text((40, 450), f"TELEFONE: {get_patient_value(patient_data, 'nr_fone', '-')}", fill="black", font=base_font)
    draw.text((40, 480), f"FILIAÇÃO: {get_patient_value(patient_data, 'nm_mae', '-')} / {get_patient_value(patient_data, 'nm_pai', '-')}", fill="black", font=base_font)

    draw.text((390, 560), "TERMO DE RESPONSABILIDADE E CONSENTIMENTO", fill="black", font=base_font)
    draw.multiline_text(
        (40, 600),
        "1 - Autorizo os procedimentos necessários ao meu atendimento.\n"
        "2 - Declaro estar ciente das orientações médicas e da documentação apresentada.\n"
        "3 - O hospital não se responsabiliza por objetos pessoais.",
        fill="black",
        font=base_font,
        spacing=8,
    )
    draw.text((70, 1110), "Assin Paciente ou Responsável: ________________________________", fill="black", font=base_font)

    target_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(target_pdf_path, "PDF", resolution=SIGNATURE_BASE_PDF_DPI)



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

        CREATE TABLE IF NOT EXISTS signature_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            page_index INTEGER NOT NULL DEFAULT 0,
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            width_ratio REAL NOT NULL,
            height_ratio REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );


        CREATE TABLE IF NOT EXISTS controle_pacientes (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         atendimento TEXT UNIQUE,
         status TEXT CHECK(status IN ('pendente', 'vigilancia', 'lancado')) DEFAULT 'pendente',
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
    # 🔥 OCR SETTINGS
    ocr_settings = db.execute(
        "SELECT id FROM ocr_settings WHERE id = 1"
    ).fetchone()

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

    # 🔥 SIGNATURE SETTINGS
    signature_settings = db.execute(
        "SELECT id FROM signature_settings WHERE id = 1"
    ).fetchone()

    if signature_settings is None:
        db.execute(
            """
            INSERT INTO signature_settings (id, page_index, x_ratio, y_ratio, width_ratio, height_ratio)
            VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                SIGNATURE_BOX_DEFAULT["page_index"],
                SIGNATURE_BOX_DEFAULT["x_ratio"],
                SIGNATURE_BOX_DEFAULT["y_ratio"],
                SIGNATURE_BOX_DEFAULT["width_ratio"],
                SIGNATURE_BOX_DEFAULT["height_ratio"],
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

def get_signature_box_settings() -> dict[str, float | int]:
    default_box = SIGNATURE_BOX_DEFAULT.copy()
    query = """
        SELECT page_index, x_ratio, y_ratio, width_ratio, height_ratio
        FROM signature_settings
        WHERE id = 1
    """

    if has_app_context():
        db = get_db()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signature_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                page_index INTEGER NOT NULL DEFAULT 0,
                x_ratio REAL NOT NULL,
                y_ratio REAL NOT NULL,
                width_ratio REAL NOT NULL,
                height_ratio REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        row = db.execute(query).fetchone()
        if row is None:
            db.execute(
                """
                INSERT INTO signature_settings (id, page_index, x_ratio, y_ratio, width_ratio, height_ratio)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    SIGNATURE_BOX_DEFAULT["page_index"],
                    SIGNATURE_BOX_DEFAULT["x_ratio"],
                    SIGNATURE_BOX_DEFAULT["y_ratio"],
                    SIGNATURE_BOX_DEFAULT["width_ratio"],
                    SIGNATURE_BOX_DEFAULT["height_ratio"],
                ),
            )
            db.commit()
            return default_box
        return {
            "page_index": row["page_index"],
            "x_ratio": row["x_ratio"],
            "y_ratio": row["y_ratio"],
            "width_ratio": row["width_ratio"],
            "height_ratio": row["height_ratio"],
        }

    db_path = Path(app.config.get("DATABASE", DATABASE))
    if not db_path.exists():
        return default_box

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS signature_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                page_index INTEGER NOT NULL DEFAULT 0,
                x_ratio REAL NOT NULL,
                y_ratio REAL NOT NULL,
                width_ratio REAL NOT NULL,
                height_ratio REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        row = db.execute(query).fetchone()
        if row is None:
            db.execute(
                """
                INSERT INTO signature_settings (id, page_index, x_ratio, y_ratio, width_ratio, height_ratio)
                VALUES (1, ?, ?, ?, ?, ?)
                """,
                (
                    SIGNATURE_BOX_DEFAULT["page_index"],
                    SIGNATURE_BOX_DEFAULT["x_ratio"],
                    SIGNATURE_BOX_DEFAULT["y_ratio"],
                    SIGNATURE_BOX_DEFAULT["width_ratio"],
                    SIGNATURE_BOX_DEFAULT["height_ratio"],
                ),
            )
            db.commit()
            return default_box
        return {
            "page_index": row["page_index"],
            "x_ratio": row["x_ratio"],
            "y_ratio": row["y_ratio"],
            "width_ratio": row["width_ratio"],
            "height_ratio": row["height_ratio"],
        }
    finally:
        db.close()



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
            bitmap = page.render(scale=SIGNED_PDF_RENDER_SCALE)
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

def to_stored_path(file_path: Path, attendance_number: str) -> str:
    try:
        return str(file_path.relative_to(BASE_DIR))
    except ValueError:
        return str(Path("storage") / "atendimentos" / attendance_number / file_path.name)


def normalize_attendance_number(value: str) -> str:
    only_digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return only_digits.lstrip("0") or "0"


def find_latest_upload_for_attendance(attendance_number: str):
    normalized = normalize_attendance_number(attendance_number)
    uploads = get_db().execute(
        """
        SELECT id, original_filename, stored_path, attendance_number, created_at
        FROM uploads
        ORDER BY id DESC
        """
    ).fetchall()

    for upload in uploads:
        if normalize_attendance_number(upload["attendance_number"]) == normalized:
            return upload
    return None

def find_latest_signed_upload_for_attendance(attendance_number: str):
    normalized = normalize_attendance_number(attendance_number)
    uploads = get_db().execute(
        """
        SELECT id, original_filename, stored_path, attendance_number, created_at
        FROM uploads
        ORDER BY created_at DESC
        """
    ).fetchall()
    for upload in uploads:
        if normalize_attendance_number(upload["attendance_number"]) != normalized:
            continue
        if str(upload["original_filename"]).lower().startswith("assinado-"):
            return upload
    return None

def decode_signature_data(signature_data_url: str):
    if not signature_data_url or "," not in signature_data_url:
        raise ValueError("Assinatura inválida. Tente assinar novamente.")
    _, encoded = signature_data_url.split(",", 1)
    try:
        signature_bytes = base64.b64decode(encoded)
    except Exception as exc:
        raise ValueError("Não foi possível processar a assinatura enviada.") from exc

    return Image.open(BytesIO(signature_bytes)).convert("RGBA")


def merge_signature_into_pdf(
    source_pdf_path: Path,
    target_pdf_path: Path,
    signature_data_url: str,
    box: dict | None = None,
):
    if pdfium is None:
        raise RuntimeError("Assinatura online indisponível: pypdfium2 não está instalado.")

    signature_image = decode_signature_data(signature_data_url)
    signature_box = box or get_signature_box_settings()

    pdf = pdfium.PdfDocument(str(source_pdf_path))
    pages = []
    output_resolution = float(PDF_EXPORT_DPI)

    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            bitmap = page.render(scale=SIGNED_PDF_RENDER_SCALE)
            page_image = bitmap.to_pil().convert("RGB")
            page_width_pt, _page_height_pt = page.get_size()

            if page_index == 0 and page_width_pt:
                output_resolution = max(72.0, float(page_image.width) * 72.0 / float(page_width_pt))

            if page_index == signature_box["page_index"]:
                overlay = Image.new("RGBA", page_image.size, (255, 255, 255, 0))
                width, height = page_image.size
                target_x = int(width * signature_box["x_ratio"])
                target_y = int(height * signature_box["y_ratio"])
                target_w = max(1, int(width * signature_box["width_ratio"]))
                target_h = max(1, int(height * signature_box["height_ratio"]))
                resized_signature = signature_image.resize((target_w, target_h))
                overlay.paste(resized_signature, (target_x, target_y), resized_signature)
                page_image = Image.alpha_composite(page_image.convert("RGBA"), overlay).convert("RGB")

            pages.append(page_image)
    finally:
        pdf.close()

    if not pages:
        raise ValueError("O PDF informado não possui páginas para assinar.")

    pages[0].save(
        target_pdf_path,
        "PDF",
        resolution=output_resolution,
        save_all=True,
        append_images=pages[1:],
    )

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
    raw_value = attendance_number.strip()
    original_digits = "".join(ch for ch in raw_value if ch.isdigit())
    normalized_str = raw_value.lstrip("0") or "0"

    print("DEBUG atendimento:", repr(normalized_str))

    return {
        "attendance_number": int(normalized_str),
        "attendance_number_str": normalized_str,
        "attendance_number_num": int(normalized_str),  # 🔥 ESSENCIAL
        "attendance_length": len(original_digits) if original_digits else len(raw_value),
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

        bind_names = set(BIND_PARAM_PATTERN.findall(query))
        filtered_params = {k: v for k, v in params.items() if k in bind_names}

        cursor.execute(query, filtered_params)

        rows = cursor.fetchall()

        print("TOTAL DE LINHAS:", len(rows))

        columns = [column[0].lower() for column in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    except Exception as e:
        print("🔥 ERRO ORACLE:", str(e))
        raise

    finally:
        connection.close()


def safe_run_oracle_query(query: str, attendance_number: str, label: str) -> tuple[list[dict], str | None]:
    try:
          return run_oracle_query(query, attendance_number), None
    except Exception as exc:
           return [], f"{label}: {exc}"


def build_cid_label(row: dict | None) -> str | None:
    if not row:
        return None

    code = str(row.get("cd_cid") or "").strip()
    description = str(row.get("ds_cid") or "").strip()

    if code and description:
        return f"{code} - {description}"
    if code:
        return code
    if description:
        return description
    return None


def enrich_rows_with_cid(rows: list[dict]) -> list[dict]:
    enriched_rows = []
    for row in rows:
        enriched = dict(row)
        enriched["cid_label"] = build_cid_label(enriched)
        enriched_rows.append(enriched)
    return enriched_rows


def fetch_attendance_context(attendance_number: str) -> dict:
    print("DEBUG atendimento:", attendance_number)

    prescriptions, prescriptions_error = safe_run_oracle_query(
        PRESCRIPTION_QUERY,
        attendance_number,
        "Prescrições",
    )
    attendance_rows, attendance_error = safe_run_oracle_query(
        ATTENDANCE_CONTEXT_QUERY,
        attendance_number,
        "Resumo clínico",
    )
    lab_exams, lab_error = safe_run_oracle_query(
        LAB_EXAMS_QUERY,
        attendance_number,
        "Exames laboratoriais",
    )
    imaging_exams, imaging_error = safe_run_oracle_query(
        IMAGING_EXAMS_QUERY,
        attendance_number,
        "Exames de imagem",
    )

    prescriptions = enrich_rows_with_cid(prescriptions)
    attendance_rows = enrich_rows_with_cid(attendance_rows)
    lab_exams = enrich_rows_with_cid(lab_exams)
    imaging_exams = enrich_rows_with_cid(imaging_exams)

    summary = attendance_rows[0] if attendance_rows else None
    if summary:
        summary = dict(summary)
        summary["cid_label"] = build_cid_label(summary)

    errors = [error for error in [prescriptions_error, attendance_error, lab_error, imaging_error] if error]

    return {
        "prescriptions": prescriptions,
        "attendance_rows": attendance_rows,
        "lab_exams": lab_exams,
        "imaging_exams": imaging_exams,
        "summary": summary,
        "error": " | ".join(errors) if errors else None,
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

@app.route("/reports")
@login_required
@role_required("admin", "faturamento")
def reports():
    inicio = request.args.get("inicio")
    fim = request.args.get("fim")

    filtro = ""

    if inicio and fim:
        filtro = f"""
        AND A.DT_ATENDIMENTO BETWEEN 
        TO_DATE('{inicio}','YYYY-MM-DD') 
        AND TO_DATE('{fim}','YYYY-MM-DD')
        """

    # 🔥 DAILY
    daily_query = f"""
    SELECT
        TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY') AS DATA,
        COUNT(*) AS TOTAL
    FROM DBAMV.ATENDIME A
    WHERE A.TP_ATENDIMENTO = 'U'
    {filtro}
    GROUP BY TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY')
    ORDER BY DATA
    """

    # 🔥 MONTHLY
    monthly_query = f"""
    SELECT
        TO_CHAR(A.DT_ATENDIMENTO, 'MM/YYYY') AS MES,
        COUNT(*) AS TOTAL
    FROM DBAMV.ATENDIME A
    WHERE A.TP_ATENDIMENTO = 'U'
    {filtro}
    GROUP BY TO_CHAR(A.DT_ATENDIMENTO, 'MM/YYYY')
    ORDER BY MES
    """

    # 🔥 YEARLY
    yearly_query = f"""
    SELECT
        TO_CHAR(A.DT_ATENDIMENTO, 'YYYY') AS ANO,
        COUNT(*) AS TOTAL
    FROM DBAMV.ATENDIME A
    WHERE A.TP_ATENDIMENTO = 'U'
    {filtro}
    GROUP BY TO_CHAR(A.DT_ATENDIMENTO, 'YYYY')
    ORDER BY ANO
    """

    # 🔥 DETALHADO (PACIENTES)
    detalhado_query = f"""
    SELECT
        PA.NM_PACIENTE,
        A.CD_ATENDIMENTO AS NUMERO_ATENDIMENTO,
        TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY HH24:MI') AS DATA_ATENDIMENTO
    FROM DBAMV.ATENDIME A
    LEFT JOIN DBAMV.PACIENTE PA ON PA.CD_PACIENTE = A.CD_PACIENTE
    WHERE A.TP_ATENDIMENTO = 'U'
    {filtro}
    ORDER BY A.DT_ATENDIMENTO DESC
    """

    daily = run_oracle_query(daily_query, "0")
    monthly = run_oracle_query(monthly_query, "0")
    yearly = run_oracle_query(yearly_query, "0")
    detalhado = run_oracle_query(detalhado_query, "0")
    # 🔥 PEGAR ATENDIMENTOS COM PDF (SQLite)
    db = get_db()
    uploads_db = db.execute("SELECT attendance_number FROM uploads").fetchall()

    uploads_set = {
        str(u["attendance_number"]).lstrip("0")
        for u in uploads_db
}

    # 🔥 MARCAR NO DETALHADO
    for d in detalhado:
        d["tem_pdf"] = str(d["numero_atendimento"]).lstrip("0") in uploads_set

    return render_template(
        "reports.html",
        daily=daily,
        monthly=monthly,
        yearly=yearly,
        detalhado=detalhado
    )


@app.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    user = current_user()

    # 🔥 ÚLTIMOS UPLOADS
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

    # 🔥 CONTADORES
    user_count = db.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
    upload_count = db.execute("SELECT COUNT(*) AS total FROM uploads").fetchone()["total"]

    # 🔥 PACIENTES (ORACLE)
    patients_query = """
    SELECT
        A.CD_ATENDIMENTO,
        PA.NM_PACIENTE,
        TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY HH24:MI') AS DATA
    FROM DBAMV.ATENDIME A
    LEFT JOIN DBAMV.PACIENTE PA ON PA.CD_PACIENTE = A.CD_PACIENTE
    WHERE A.TP_ATENDIMENTO = 'U'
    AND A.DT_ATENDIMENTO >= SYSDATE - 1
    ORDER BY A.DT_ATENDIMENTO DESC
    """

    try:
        patients = run_oracle_query(patients_query, "0")
    except Exception as e:
        print("Erro ao buscar pacientes:", e)
        patients = []

    # 🔥 PEGAR ATENDIMENTOS QUE JÁ TEM PDF
    uploads_db = db.execute("SELECT attendance_number FROM uploads").fetchall()

    uploads_set = {
     str(u["attendance_number"]).lstrip("0")
     for u in uploads_db
}

    for p in patients:
     p["tem_pdf"] = str(p["cd_atendimento"]).lstrip("0") in uploads_set

    # 🔥 RENDER
    return render_template(
        "dashboard.html",
        uploads=uploads,
        user=user,
        user_count=user_count,
        upload_count=upload_count,
        patients=patients
    )


@app.route("/upload", methods=["GET", "POST"])
@login_required
@role_required("recepcao", "admin")
def upload_pdf():
    selected_attendance = request.args.get("atendimento", "").strip()
    selected_upload = None
    selected_signed_upload = None
    patient_data = None

    if selected_attendance:
        patient_rows, _ = safe_run_oracle_query(
            ONLINE_SIGNATURE_QUERY,
            selected_attendance,
            "Dados do paciente",
        )
        patient_data = patient_rows[0] if patient_rows else None

        preview_path = Path(app.config["UPLOAD_ROOT"]) / selected_attendance / f"{selected_attendance}-signature-base.pdf"
        generate_signature_base_pdf(selected_attendance, patient_data, preview_path)
        selected_upload = {
            "stored_path": to_stored_path(preview_path, selected_attendance),
            "attendance_number": selected_attendance,
        }
        
        selected_signed_upload = find_latest_signed_upload_for_attendance(selected_attendance)

    if request.method == "POST":
        action = request.form.get("action", "upload_pdf")

        if action == "online_signature":
            attendance_number = request.form.get("attendance_number", "").strip()
            signature_data = request.form.get("signature_data", "")

            if not attendance_number:
                flash("Informe o número do atendimento para assinar.", "danger")
                return redirect(url_for("upload_pdf"))

            source_upload = find_latest_upload_for_attendance(attendance_number)
            source_pdf_path = None
            if source_upload:
                source_pdf_path = BASE_DIR / source_upload["stored_path"]
            else:
                generated_base_path = (
                    Path(app.config["UPLOAD_ROOT"])
                    / attendance_number
                    / f"{attendance_number}-signature-base.pdf"
                )
                if generated_base_path.exists():
                    source_pdf_path = generated_base_path
                else:
                    flash("Nenhum PDF base foi encontrado para o atendimento informado.", "danger")
                    return redirect(url_for("upload_pdf", atendimento=attendance_number))

            if not source_pdf_path.exists():
                configured_root = Path(app.config["UPLOAD_ROOT"])
                source_fallback = (
                    configured_root
                    / attendance_number
                    / Path(source_pdf_path).name
                )
                if source_fallback.exists():
                    source_pdf_path = source_fallback
            if not source_pdf_path.exists():
                flash("O PDF base do atendimento não foi encontrado no armazenamento.", "danger")
                return redirect(url_for("upload_pdf", atendimento=attendance_number))

            signed_path = build_storage_path(attendance_number)
            try:
                merge_signature_into_pdf(source_pdf_path, signed_path, signature_data)
                get_db().execute(
                    """
                    INSERT INTO uploads (original_filename, stored_path, attendance_number, uploaded_by)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        f"assinado-{source_pdf_path.name}",
                        to_stored_path(signed_path, attendance_number),
                        attendance_number,
                        current_user()["id"],
                    ),
                )
                get_db().commit()
                flash("Documento assinado e enviado com sucesso.", "success")
            except Exception as exc:
                if signed_path.exists():
                    signed_path.unlink()
                flash(str(exc), "danger")

            return redirect(url_for("upload_pdf", atendimento=attendance_number))

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
                    to_stored_path(final_path, attendance_number),
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
    return render_template(
        "upload.html",
        selected_attendance=selected_attendance,
        selected_upload=selected_upload,
        selected_signed_upload=selected_signed_upload,
        patient_data=patient_data,
         signature_box=get_signature_box_settings(),
    )


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
        lab_exams=context["lab_exams"],
        imaging_exams=context["imaging_exams"],
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
    active_tab = request.args.get("tab", "ocr")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS signature_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            page_index INTEGER NOT NULL DEFAULT 0,
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            width_ratio REAL NOT NULL,
            height_ratio REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    if request.method == "POST":
        action = request.form.get("action", "save_ocr")

        if action == "save_signature":
            page_index = request.form.get("page_index", type=int)
            x_percent = request.form.get("signature_x_percent", type=float)
            y_percent = request.form.get("signature_y_percent", type=float)
            width_percent = request.form.get("signature_width_percent", type=float)
            height_percent = request.form.get("signature_height_percent", type=float)

            values = [x_percent, y_percent, width_percent, height_percent]
            if page_index is None or page_index < 0 or any(value is None for value in values):
                flash("Preencha todos os campos da área da assinatura.", "danger")
                return redirect(url_for("manage_ocr_settings", tab="signature"))

            if (
                x_percent < 0
                or y_percent < 0
                or width_percent <= 0
                or height_percent <= 0
                or x_percent + width_percent > 100
                or y_percent + height_percent > 100
            ):
                flash("A área da assinatura deve ficar dentro de 0% a 100% da página.", "danger")
                return redirect(url_for("manage_ocr_settings", tab="signature"))

            db.execute(
                """
                INSERT INTO signature_settings (id, page_index, x_ratio, y_ratio, width_ratio, height_ratio, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    page_index = excluded.page_index,
                    x_ratio = excluded.x_ratio,
                    y_ratio = excluded.y_ratio,
                    width_ratio = excluded.width_ratio,
                    height_ratio = excluded.height_ratio,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    page_index,
                    x_percent / 100,
                    y_percent / 100,
                    width_percent / 100,
                    height_percent / 100,
                ),
            )
            db.commit()
            flash("Área da assinatura atualizada com sucesso.", "success")
            return redirect(url_for("manage_ocr_settings", tab="signature"))

        x_percent = request.form.get("x_percent", type=float)
        y_percent = request.form.get("y_percent", type=float)
        width_percent = request.form.get("width_percent", type=float)
        height_percent = request.form.get("height_percent", type=float)

        values = [x_percent, y_percent, width_percent, height_percent]
        if any(value is None for value in values):
            flash("Preencha todos os campos da área do OCR.", "danger")
            return redirect(url_for("manage_ocr_settings", tab="ocr"))

        if (
            x_percent < 0
            or y_percent < 0
            or width_percent <= 0
            or height_percent <= 0
            or x_percent + width_percent > 100
            or y_percent + height_percent > 100
        ):
            flash("A área do OCR deve ficar dentro de 0% a 100% da página.", "danger")
            return redirect(url_for("manage_ocr_settings", tab="ocr"))

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
        return redirect(url_for("manage_ocr_settings", tab="ocr"))

    region = get_ocr_capture_region()
    signature_box = get_signature_box_settings()
    return render_template(
        "ocr_settings.html",
        active_tab=active_tab if active_tab in {"ocr", "signature"} else "ocr",
        region={
            "x_percent": round(region["x"] * 100, 2),
            "y_percent": round(region["y"] * 100, 2),
            "width_percent": round(region["width"] * 100, 2),
            "height_percent": round(region["height"] * 100, 2),
        },
        signature_region={
            "page_index": int(signature_box["page_index"]),
            "x_percent": round(signature_box["x_ratio"] * 100, 2),
            "y_percent": round(signature_box["y_ratio"] * 100, 2),
            "width_percent": round(signature_box["width_ratio"] * 100, 2),
            "height_percent": round(signature_box["height_ratio"] * 100, 2),
        },
    )


@app.route("/files/<path:relative_path>")
@login_required
@role_required("recepcao", "faturamento", "admin")
def serve_file(relative_path: str):
    file_path = BASE_DIR / relative_path
    return send_from_directory(file_path.parent, file_path.name, as_attachment=False)

@app.route("/controle_pacientes", methods=["GET", "POST"])
@login_required
@role_required("faturamento", "admin")
def controle_pacientes():
    db = get_db()

    # garantir tabela
    db.execute("""
    CREATE TABLE IF NOT EXISTS controle_pacientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        atendimento TEXT UNIQUE,
        vigilancia INTEGER DEFAULT 0,
        lancado INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.commit()

    cursor = db.execute("PRAGMA table_info(controle_pacientes)")
    cols = [c[1] for c in cursor.fetchall()]

    if "vigilancia" not in cols:
        db.execute("ALTER TABLE controle_pacientes ADD COLUMN vigilancia INTEGER DEFAULT 0")

    if "lancado" not in cols:
     db.execute("ALTER TABLE controle_pacientes ADD COLUMN lancado INTEGER DEFAULT 0")

    db.commit()

    # 🔥 AÇÕES
    if request.method == "POST":
        atendimento = request.form.get("atendimento")
        acao = request.form.get("acao")

        atendimento = str(atendimento).lstrip("0")

        db.execute("""
            INSERT INTO controle_pacientes (atendimento)
            VALUES (?)
            ON CONFLICT(atendimento) DO NOTHING
        """, (atendimento,))

        if acao == "vigilancia_on":
            db.execute("UPDATE controle_pacientes SET vigilancia = 1 WHERE atendimento = ?", (atendimento,))

        elif acao == "vigilancia_off":
            db.execute("UPDATE controle_pacientes SET vigilancia = 0 WHERE atendimento = ?", (atendimento,))

        elif acao == "lancado":
            db.execute("UPDATE controle_pacientes SET lancado = 1 WHERE atendimento = ?", (atendimento,))

        db.commit()
        return redirect(url_for("controle_pacientes"))

    # 🔥 PACIENTES
    query = """
    SELECT
        A.CD_ATENDIMENTO,
        PA.NM_PACIENTE,
        TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY HH24:MI') AS DATA
    FROM DBAMV.ATENDIME A
    LEFT JOIN DBAMV.PACIENTE PA ON PA.CD_PACIENTE = A.CD_PACIENTE
    WHERE A.TP_ATENDIMENTO = 'U'
    AND A.DT_ATENDIMENTO >= SYSDATE - 2
    ORDER BY A.DT_ATENDIMENTO DESC
    """

    try:
        pacientes = run_oracle_query(query, "0")
    except:
        pacientes = []

    # 🔥 STATUS
    status_db = db.execute("SELECT * FROM controle_pacientes").fetchall()

    status_map = {
        str(s["atendimento"]).lstrip("0"): s
        for s in status_db
    }

    resultado = []

    for p in pacientes:
        atendimento = str(p["cd_atendimento"]).lstrip("0")
        status = status_map.get(atendimento)

        vigilancia = status["vigilancia"] if status and "vigilancia" in status.keys() else 0
        lancado = status["lancado"] if status and "lancado" in status.keys() else 0

        # 🔥 SE LANÇADO → NÃO MOSTRA
        if lancado == 1:
            continue

        p["vigilancia"] = vigilancia
        p["lancado"] = lancado

        resultado.append(p)

    return render_template(
        "controle_pacientes.html",
        pacientes=resultado
    )

from collections import defaultdict

@app.route("/controle_relatorio")
@login_required
@role_required("faturamento", "admin")
def controle_relatorio():
    db = get_db()

    # 🔥 GARANTIR TABELA CORRETA
    db.execute("""
    CREATE TABLE IF NOT EXISTS controle_pacientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        atendimento TEXT UNIQUE,
        vigilancia INTEGER DEFAULT 0,
        lancado INTEGER DEFAULT 0,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    db.commit()

    # 🔥 BUSCAR STATUS
    status_db = db.execute("""
        SELECT atendimento, vigilancia, lancado
        FROM controle_pacientes
    """).fetchall()

    status_map = {
        str(s["atendimento"]).lstrip("0"): s
        for s in status_db
    }

    # 🔥 ORACLE
    query = """
    SELECT
        A.CD_ATENDIMENTO,
        PA.NM_PACIENTE,
        TO_CHAR(A.DT_ATENDIMENTO, 'MM/YYYY') AS MES,
        TO_CHAR(A.DT_ATENDIMENTO, 'DD/MM/YYYY HH24:MI') AS DATA
    FROM DBAMV.ATENDIME A
    LEFT JOIN DBAMV.PACIENTE PA ON PA.CD_PACIENTE = A.CD_PACIENTE
    WHERE A.TP_ATENDIMENTO = 'U'
    AND A.DT_ATENDIMENTO >= ADD_MONTHS(TRUNC(SYSDATE,'MM'), -3)
    ORDER BY A.DT_ATENDIMENTO DESC
    """

    try:
        pacientes = run_oracle_query(query, "0")
    except:
        pacientes = []

    # 🔥 AGRUPAR
    agrupado = defaultdict(lambda: {
        "lancados": [],
        "vigilancia": []
    })

    for p in pacientes:
        atendimento = str(p["cd_atendimento"]).lstrip("0")
        status = status_map.get(atendimento)

        if not status:
            continue

        mes = p["mes"]

        if status["lancado"] == 1:
            agrupado[mes]["lancados"].append(p)

        if status["vigilancia"] == 1:
            agrupado[mes]["vigilancia"].append(p)

    return render_template(
        "controle_relatorio.html",
        agrupado=agrupado
    )


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
    if not file_path.exists():
        configured_root = Path(app.config["UPLOAD_ROOT"])
        fallback_path = configured_root / upload["attendance_number"] / Path(upload["stored_path"]).name
        if fallback_path.exists():
            file_path = fallback_path
    if file_path.exists():
        file_path.unlink()
        parent_dir = file_path.parent
        if parent_dir.exists() and not any(parent_dir.iterdir()):
            parent_dir.rmdir()

    db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))# 🔥 MARCAR PACIENTES COM PDF
    db.commit()
    flash("Arquivo excluído com sucesso.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
