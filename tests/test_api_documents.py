"""Document upload, listing, and ownership through the real FastAPI app."""

from app.services import document_service
from tests.conftest import auth_headers


def _upload(client, user, filename: str, content: bytes):
    return client.post(
        "/documents/upload", files={"file": (filename, content)}, headers=auth_headers(user)
    )


class TestUpload:
    def test_upload_is_classified_from_filename(self, client, storage, patient):
        resp = client.post(
            "/documents/upload",
            files={"file": ("ecg_report.pdf", b"fake ecg bytes")},
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["document_type"] == "ecg_report"
        assert body["classification_confidence"] > 0
        assert body["is_duplicate_of"] is None

    def test_second_upload_of_identical_content_is_flagged_as_duplicate(self, client, storage, patient):
        first = _upload(client, patient.user, "scan1.pdf", b"same bytes")
        second = _upload(client, patient.user, "scan2.pdf", b"same bytes")
        assert second.json()["is_duplicate_of"] == first.json()["id"]

    def test_oversized_file_is_rejected(self, client, storage, patient, monkeypatch):
        monkeypatch.setattr(document_service, "max_file_bytes", lambda: 10)
        resp = client.post(
            "/documents/upload",
            files={"file": ("big.pdf", b"more than ten bytes of content")},
            headers=auth_headers(patient.user),
        )
        assert resp.status_code == 422

    def test_staff_cannot_upload_on_the_patient_route(self, client, storage, actor):
        resp = _upload(client, actor, "x.pdf", b"data")
        assert resp.status_code == 403


class TestListAndOwnership:
    def test_uploaded_document_appears_in_my_list(self, client, storage, patient):
        _upload(client, patient.user, "blood_test.pdf", b"blood data")
        listed = client.get("/documents/me", headers=auth_headers(patient.user))
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        assert listed.json()[0]["document_type"] == "blood_report"

    def test_patient_cannot_read_another_patients_document(self, client, storage, patient, other_patient):
        upload = _upload(client, patient.user, "x.pdf", b"data")
        doc_id = upload.json()["id"]

        resp = client.get(f"/documents/{doc_id}", headers=auth_headers(other_patient.user))
        assert resp.status_code == 404

    def test_staff_can_read_any_patients_document(self, client, storage, patient, actor):
        upload = _upload(client, patient.user, "x.pdf", b"data")
        doc_id = upload.json()["id"]

        resp = client.get(f"/documents/{doc_id}", headers=auth_headers(actor))
        assert resp.status_code == 200


class TestSummarize:
    def test_patient_can_summarize_own_document(self, client, storage, patient, monkeypatch):
        from app.services import document_service as ds

        monkeypatch.setattr(ds, "summarize_document", lambda db, **kw: ds.get_document(db, kw["document_id"]))
        upload = _upload(client, patient.user, "notes.txt", b"some text")
        doc_id = upload.json()["id"]

        resp = client.post(f"/documents/{doc_id}/summarize", headers=auth_headers(patient.user))
        assert resp.status_code == 200

    def test_patient_cannot_summarize_another_patients_document(
        self, client, storage, patient, other_patient
    ):
        upload = _upload(client, patient.user, "notes.txt", b"some text")
        doc_id = upload.json()["id"]

        resp = client.post(f"/documents/{doc_id}/summarize", headers=auth_headers(other_patient.user))
        assert resp.status_code == 404

    def test_staff_can_summarize_any_patients_document(self, client, storage, patient, actor, monkeypatch):
        from app.services import document_service as ds

        monkeypatch.setattr(ds, "summarize_document", lambda db, **kw: ds.get_document(db, kw["document_id"]))
        upload = _upload(client, patient.user, "notes.txt", b"some text")
        doc_id = upload.json()["id"]

        resp = client.post(f"/documents/{doc_id}/summarize", headers=auth_headers(actor))
        assert resp.status_code == 200


class TestMissingDocuments:
    def test_missing_documents_reflects_department_requirements(self, client, storage, patient, cardiology):
        resp = client.get(
            "/documents/missing", params={"department_id": cardiology.id}, headers=auth_headers(patient.user)
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["department_name"] == "Cardiology"
        assert set(body["missing"]) == set(body["required"])

        _upload(client, patient.user, "ecg_report.pdf", b"ecg")
        after = client.get(
            "/documents/missing", params={"department_id": cardiology.id}, headers=auth_headers(patient.user)
        ).json()
        assert "ecg_report" not in after["missing"]
