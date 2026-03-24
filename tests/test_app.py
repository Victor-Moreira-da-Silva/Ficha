from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import app as ficha_app


class FichaAppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.upload_root = Path(self.tmpdir.name) / "uploads"

        ficha_app.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            DATABASE=str(self.db_path),
            UPLOAD_ROOT=str(self.upload_root),
        )
        self.upload_root.mkdir(parents=True, exist_ok=True)
        ficha_app.init_db()
        self.client = ficha_app.app.test_client()

    def tearDown(self):
        self.tmpdir.cleanup()

    def login(self, username, password):
        return self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=True,
        )

    def create_user(self, username, password, role):
        with ficha_app.app.app_context():
            db = ficha_app.get_db()
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, ficha_app.generate_password_hash(password), role),
            )
            db.commit()

    def test_admin_can_create_and_change_password(self):
        self.login("admin", "admin123")
        response = self.client.post(
            "/admin/users",
            data={
                "action": "create",
                "username": "recep1",
                "password": "senha123",
                "role": "recepcao",
            },
            follow_redirects=True,
        )
        self.assertIn("Usuário criado com sucesso".encode(), response.data)

        with ficha_app.app.app_context():
            user = ficha_app.get_db().execute(
                "SELECT id FROM users WHERE username = ?", ("recep1",)
            ).fetchone()

        response = self.client.post(
            "/admin/users",
            data={
                "action": "change_password",
                "user_id": user["id"],
                "new_password": "nova-senha",
            },
            follow_redirects=True,
        )
        self.assertIn("Senha atualizada com sucesso".encode(), response.data)

        self.client.get("/logout", follow_redirects=True)
        response = self.login("recep1", "nova-senha")
        self.assertIn("Bem-vindo".encode(), response.data)

    def test_recepcao_uploads_pdf_to_attendance_folder(self):
        self.create_user("recep", "senha123", "recepcao")
        self.login("recep", "senha123")

        original_extractor = ficha_app.extract_attendance_number
        ficha_app.extract_attendance_number = lambda _path: "998877"
        try:
            response = self.client.post(
                "/upload",
                data={"pdf_file": (BytesIO(b"fake pdf content"), "guia.pdf")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
        finally:
            ficha_app.extract_attendance_number = original_extractor

        self.assertIn("998877".encode(), response.data)
        stored_file = self.upload_root / "998877" / "998877.pdf"
        self.assertTrue(stored_file.exists())

    def test_resolve_tesseract_cmd_accepts_install_folder_in_env(self):
        original_env = ficha_app.os.environ.get("TESSERACT_CMD")
        original_which = ficha_app.shutil.which
        original_locations = ficha_app.WINDOWS_TESSERACT_LOCATIONS
        install_dir = Path(self.tmpdir.name) / "Tesseract-OCR"
        install_dir.mkdir(parents=True, exist_ok=True)
        exe = install_dir / "tesseract.exe"
        exe.write_text("fake exe")

        ficha_app.os.environ["TESSERACT_CMD"] = str(install_dir)
        ficha_app.shutil.which = lambda _cmd: None
        ficha_app.WINDOWS_TESSERACT_LOCATIONS = []

        try:
            resolved = ficha_app.resolve_tesseract_cmd()
        finally:
            if original_env is None:
                ficha_app.os.environ.pop("TESSERACT_CMD", None)
            else:
                ficha_app.os.environ["TESSERACT_CMD"] = original_env
            ficha_app.shutil.which = original_which
            ficha_app.WINDOWS_TESSERACT_LOCATIONS = original_locations

        self.assertEqual(resolved, str(exe))

    def test_build_storage_path_uses_attendance_number_and_suffix_for_duplicates(self):
        with ficha_app.app.app_context():
            first_path = ficha_app.build_storage_path("445566")
            first_path.parent.mkdir(parents=True, exist_ok=True)
            first_path.write_bytes(b"pdf")
            second_path = ficha_app.build_storage_path("445566")

        self.assertEqual(first_path.name, "445566.pdf")
        self.assertEqual(second_path.name, "445566-2.pdf")

    def test_build_oracle_params_normalizes_leading_zeros(self):
        params = ficha_app.build_oracle_params("00180627")

        self.assertEqual(params["attendance_number_str"], "180627")
        self.assertEqual(params["attendance_number_num"], 180627)
        self.assertEqual(params["attendance_length"], 8)

    def test_extract_attendance_number_prioritizes_focused_area_before_ocr(self):
        original_text_layer = ficha_app.extract_text_layer_from_pdf
        original_ocr = ficha_app.extract_text_with_ocr

        def fake_text_layer(_path, focused=False):
            return "Atendimento: 445566" if focused else "Documento com muitos números 111 222 333"

        ficha_app.extract_text_layer_from_pdf = fake_text_layer
        ficha_app.extract_text_with_ocr = lambda _path, focused=False: (_ for _ in ()).throw(
            AssertionError("OCR não deveria ser chamado")
        )
        try:
            number = ficha_app.extract_attendance_number(Path("arquivo.pdf"))
        finally:
            ficha_app.extract_text_layer_from_pdf = original_text_layer
            ficha_app.extract_text_with_ocr = original_ocr

        self.assertEqual(number, "445566")

    def test_admin_can_delete_uploaded_file(self):
        self.login("admin", "admin123")
        target_dir = self.upload_root / "123456"
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_file = target_dir / "123456.pdf"
        stored_file.write_bytes(b"pdf")

        with ficha_app.app.app_context():
            db = ficha_app.get_db()
            admin = db.execute(
                "SELECT id FROM users WHERE username = ?",
                ("admin",),
            ).fetchone()
            db.execute(
                """
                INSERT INTO uploads (original_filename, stored_path, attendance_number, uploaded_by)
                VALUES (?, ?, ?, ?)
                """,
                ("arquivo.pdf", "storage/atendimentos/123456/123456.pdf", "123456", admin["id"]),
            )
            db.commit()
            upload = db.execute(
                "SELECT id FROM uploads WHERE attendance_number = ?",
                ("123456",),
            ).fetchone()

        response = self.client.post(
            f"/uploads/{upload['id']}/delete",
            follow_redirects=True,
        )

        self.assertIn("Arquivo excluído com sucesso".encode(), response.data)
        self.assertFalse(stored_file.exists())

    def test_admin_can_access_ocr_settings_page(self):
        self.login("admin", "admin123")
        response = self.client.get("/admin/ocr-settings", follow_redirects=True)
        self.assertIn("Configurar área de captura do OCR".encode(), response.data)

    def test_admin_can_access_upload_page(self):
        self.login("admin", "admin123")
        response = self.client.get("/upload", follow_redirects=True)
        self.assertIn("Enviar PDF para OCR".encode(), response.data)

        self.assertIn("Assinatura eletrônica".encode(), response.data)

    def test_recepcao_can_generate_online_signed_pdf_from_existing_upload(self):
        self.create_user("recep_assina", "senha123", "recepcao")
        self.login("recep_assina", "senha123")

        source_dir = self.upload_root / "778899"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_file = source_dir / "778899.pdf"
        source_file.write_bytes(b"%PDF-1.4 base")

        with ficha_app.app.app_context():
            db = ficha_app.get_db()
            user = db.execute(
                "SELECT id FROM users WHERE username = ?",
                ("recep_assina",),
            ).fetchone()
            db.execute(
                """
                INSERT INTO uploads (original_filename, stored_path, attendance_number, uploaded_by)
                VALUES (?, ?, ?, ?)
                """,
                ("base.pdf", "storage/atendimentos/778899/778899.pdf", "778899", user["id"]),
            )
            db.commit()

        original_merge = ficha_app.merge_signature_into_pdf
        ficha_app.merge_signature_into_pdf = lambda _src, dst, _sig: dst.write_bytes(b"%PDF-1.4 signed")
        try:
            response = self.client.post(
                "/upload",
                data={
                    "action": "online_signature",
                    "attendance_number": "778899",
                    "signature_data": "data:image/png;base64,ZmFrZQ==",
                },
                follow_redirects=True,
            )
        finally:
            ficha_app.merge_signature_into_pdf = original_merge

        self.assertIn("Documento assinado e enviado com sucesso".encode(), response.data)

        with ficha_app.app.app_context():
            rows = ficha_app.get_db().execute(
                "SELECT COUNT(*) AS total FROM uploads WHERE attendance_number = ?",
                ("778899",),
            ).fetchone()
        self.assertEqual(rows["total"], 2)

    def test_admin_users_page_renders_after_click(self):
        self.login("admin", "admin123")
        response = self.client.get("/admin/users", follow_redirects=True)
        self.assertIn("Cadastro de usuários".encode(), response.data)

    def test_attendance_details_show_requested_exams_doctor_and_cid(self):
        self.login("admin", "admin123")
        original_fetch = ficha_app.fetch_attendance_context
        ficha_app.fetch_attendance_context = lambda _attendance: {
            "prescriptions": [
                {
                    "nm_paciente": "Maria Souza",
                    "data_hora": "20/03/2026 10:15",
                    "ds_produto": "Dipirona",
                    "qt_solicitado": 1,
                    "cd_cor_referencia": "VERDE",
                    "cd_for_apl": "VO",
                    "medico_solicitante": "Dr. João Lima",
                    "cid_label": "J11 - Influenza",
                }
            ],
            "attendance_rows": [
                {
                    "nm_paciente": "Maria Souza",
                    "cd_paciente": "123",
                    "dt_atendimento": "20/03/2026",
                    "classificacao_risco": "VERDE",
                    "queixa_principal": "Febre",
                    "temperatura": "37.8",
                    "frequencia_cardiaca": "80",
                    "pressao_sistolica": "120",
                    "pas_pad": "80",
                    "spo2": "98",
                    "glicemia": "90",
                    "medicamento": "Dipirona",
                    "medico_solicitante": "Dr. João Lima",
                    "cid_label": "J11 - Influenza",
                }
            ],
            "lab_exams": [
                {
                    "nm_paciente": "Maria Souza",
                    "data_hora": "20/03/2026 10:20",
                    "exame": "Hemograma",
                    "medico_solicitante": "Dra. Ana Costa",
                    "cid_label": "J11 - Influenza",
                }
            ],
            "imaging_exams": [
                {
                    "nm_paciente": "Maria Souza",
                    "data_hora": "20/03/2026 10:30",
                    "exame": "Raio-X de tórax",
                    "medico_solicitante": "Dr. Pedro Alves",
                    "cid_label": "J11 - Influenza",
                }
            ],
            "summary": {
                "nm_paciente": "Maria Souza",
                "cd_paciente": "123",
                "dt_atendimento": "20/03/2026",
                "classificacao_risco": "VERDE",
                "queixa_principal": "Febre",
                "temperatura": "37.8",
                "frequencia_cardiaca": "80",
                "pressao_sistolica": "120",
                "pas_pad": "80",
                "spo2": "98",
                "glicemia": "90",
                "cid_label": "J11 - Influenza",
            },
            "error": None,
        }
        try:
            response = self.client.get("/attendance/123456", follow_redirects=True)
        finally:
            ficha_app.fetch_attendance_context = original_fetch

        self.assertIn("Exames laboratoriais solicitados".encode(), response.data)
        self.assertIn("Exames de imagem solicitados".encode(), response.data)
        self.assertIn("Dr. João Lima".encode(), response.data)
        self.assertIn("Dra. Ana Costa".encode(), response.data)
        self.assertIn("J11 - Influenza".encode(), response.data)

    def test_faturamento_cannot_access_upload(self):
        self.create_user("fat", "senha123", "faturamento")
        self.login("fat", "senha123")
        response = self.client.get("/upload", follow_redirects=True)
        self.assertIn("não tem permissão".encode(), response.data.lower())


if __name__ == "__main__":
    unittest.main()
