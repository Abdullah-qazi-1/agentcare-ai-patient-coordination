"""Document classification, storage, de-duplication and gap checks.

Storage paths derive from the SHA-256 checksum rather than the uploaded filename, so
a hostile filename can never escape the storage root — that is asserted directly
below rather than assumed.
"""

from datetime import date

import pytest

from app.db.models import DocumentType
from app.services import document_service
from app.services.errors import ValidationError


class TestFilenameClassification:
    """The deterministic fast-path: the model is only called when this misses."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("ecg_report_2026_07_10.pdf", DocumentType.ECG_REPORT),
            ("EKG-scan.pdf", DocumentType.ECG_REPORT),
            ("blood_report.pdf", DocumentType.BLOOD_REPORT),
            ("cbc_results.pdf", DocumentType.BLOOD_REPORT),
            ("chest_xray.jpg", DocumentType.IMAGING_REPORT),
            ("mri_brain.dcm", DocumentType.IMAGING_REPORT),
            ("prescription_july.pdf", DocumentType.PRESCRIPTION_RECORD),
            ("discharge_summary.pdf", DocumentType.DISCHARGE_SUMMARY),
            ("insurance_card_front.png", DocumentType.INSURANCE_CARD),
            ("passport_copy.pdf", DocumentType.IDENTITY_PROOF),
            ("referral_letter.pdf", DocumentType.REFERRAL_LETTER),
        ],
    )
    def test_recognised_filenames(self, filename, expected):
        doc_type, confidence = document_service.classify_by_filename(filename)
        assert doc_type is expected
        assert confidence == 0.85

    @pytest.mark.parametrize("filename", ["document1.pdf", "img_0042.jpeg", "untitled.pdf"])
    def test_unrecognised_filename_defers_to_the_model(self, filename):
        doc_type, confidence = document_service.classify_by_filename(filename)
        assert doc_type is None
        assert confidence == 0.0

    def test_signals_match_as_substrings_not_words(self):
        """`scan001.pdf` resolves to imaging because signals are substring matches.

        Deliberate for a fast-path — catching `scan001` without a model call is the
        point — but it means a signal like `scan` is broad, and any new signal added
        to FILENAME_SIGNALS needs checking against that.
        """
        doc_type, _ = document_service.classify_by_filename("scan001.pdf")
        assert doc_type is DocumentType.IMAGING_REPORT


class TestDateExtraction:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("ecg_2026_07_10.pdf", date(2026, 7, 10)),
            ("ecg_2026-07-10.pdf", date(2026, 7, 10)),
            ("report_10-07-2026.pdf", date(2026, 7, 10)),  # day-first form
        ],
    )
    def test_extracts_dates(self, filename, expected):
        assert document_service.extract_date_from_filename(filename) == expected

    def test_no_date_returns_none(self):
        assert document_service.extract_date_from_filename("ecg_report.pdf") is None

    def test_impossible_date_returns_none(self):
        assert document_service.extract_date_from_filename("report_2026_13_45.pdf") is None


class TestStorage:
    def test_stores_and_records_metadata(self, db, patient, storage):
        doc = document_service.store_document(
            db,
            patient_id=patient.id,
            filename="ecg_report_2026_07_10.pdf",
            content=b"synthetic ecg",
            document_type=DocumentType.ECG_REPORT,
            confidence=0.85,
            document_date=date(2026, 7, 10),
        )
        assert doc.id is not None
        assert doc.document_type is DocumentType.ECG_REPORT
        assert doc.original_filename == "ecg_report_2026_07_10.pdf"
        assert doc.is_duplicate_of is None

    def test_writes_the_file_to_disk(self, db, patient, storage):
        doc = document_service.store_document(
            db, patient_id=patient.id, filename="report.pdf",
            content=b"body", document_type=DocumentType.OTHER,
        )
        assert (storage / f"patient_{patient.id}").exists()
        assert doc.storage_reference.endswith(".pdf")

    def test_storage_name_comes_from_the_checksum_not_the_filename(self, db, patient, storage):
        """A hostile filename must not be able to escape the storage root."""
        doc = document_service.store_document(
            db,
            patient_id=patient.id,
            filename="../../../etc/passwd.pdf",
            content=b"body",
            document_type=DocumentType.OTHER,
        )
        patient_dir = (storage / f"patient_{patient.id}").resolve()
        written = [p for p in patient_dir.iterdir() if p.is_file()]

        # Exactly one file, inside the patient's directory, named from the checksum.
        assert len(written) == 1
        assert written[0].stem == document_service.compute_checksum(b"body")[:16]
        assert ".." not in doc.storage_reference
        assert "etc/passwd" not in doc.storage_reference
        assert written[0].resolve().parent == patient_dir

    def test_original_filename_is_recorded_without_its_path(self, db, patient, storage):
        doc = document_service.store_document(
            db, patient_id=patient.id, filename="../../../etc/passwd.pdf",
            content=b"body", document_type=DocumentType.OTHER,
        )
        assert doc.original_filename == "passwd.pdf"

    def test_empty_file_rejected(self, db, patient, storage):
        with pytest.raises(ValidationError):
            document_service.store_document(
                db, patient_id=patient.id, filename="empty.pdf",
                content=b"", document_type=DocumentType.OTHER,
            )

    def test_oversized_file_rejected(self, db, patient, storage):
        too_big = b"x" * (document_service.max_file_bytes() + 1)
        with pytest.raises(ValidationError):
            document_service.store_document(
                db, patient_id=patient.id, filename="huge.pdf",
                content=too_big, document_type=DocumentType.OTHER,
            )

    def test_size_limit_is_configurable_via_settings(self, db, patient, storage, monkeypatch):
        """MAX_DOCUMENT_SIZE_MB is a live setting, not a hardcoded constant."""
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "max_document_size_mb", 1)
        assert document_service.max_file_bytes() == 1 * 1024 * 1024

        with pytest.raises(ValidationError):
            document_service.store_document(
                db, patient_id=patient.id, filename="just_over.pdf",
                content=b"x" * (1024 * 1024 + 1), document_type=DocumentType.OTHER,
            )


class TestDuplicateDetection:
    def test_identical_content_is_a_checksum_duplicate(self, db, patient, storage):
        content = b"identical bytes"
        first = document_service.store_document(
            db, patient_id=patient.id, filename="a.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        second = document_service.store_document(
            db, patient_id=patient.id, filename="b.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        assert second.is_duplicate_of == first.id

    def test_same_type_and_date_is_a_likely_rescan(self, db, patient, storage):
        first = document_service.store_document(
            db, patient_id=patient.id, filename="ecg_a.pdf", content=b"scan one",
            document_type=DocumentType.ECG_REPORT, document_date=date(2026, 7, 10),
        )
        result = document_service.check_duplicate(
            db,
            patient_id=patient.id,
            checksum=document_service.compute_checksum(b"scan two"),
            document_type=DocumentType.ECG_REPORT,
            document_date=date(2026, 7, 10),
        )
        assert result.is_duplicate
        assert result.match_kind == "same_type_and_date"
        assert result.existing_document_id == first.id

    def test_different_content_is_not_a_duplicate(self, db, patient, storage):
        document_service.store_document(
            db, patient_id=patient.id, filename="a.pdf",
            content=b"one", document_type=DocumentType.ECG_REPORT,
        )
        second = document_service.store_document(
            db, patient_id=patient.id, filename="b.pdf",
            content=b"two", document_type=DocumentType.BLOOD_REPORT,
        )
        assert second.is_duplicate_of is None

    def test_duplicates_are_scoped_per_patient(self, db, patient, other_patient, storage):
        """One patient's upload must never be flagged against another's."""
        content = b"same bytes"
        document_service.store_document(
            db, patient_id=patient.id, filename="a.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        theirs = document_service.store_document(
            db, patient_id=other_patient.id, filename="a.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        assert theirs.is_duplicate_of is None


class TestMissingDocuments:
    def test_reports_gaps_against_the_department_requirements(self, db, patient, cardiology, storage):
        result = document_service.check_missing_documents(
            db, patient_id=patient.id, department_id=cardiology.id
        )
        assert result.department_name == "Cardiology"
        assert set(result.missing) == {DocumentType.ECG_REPORT, DocumentType.INSURANCE_CARD}

    def test_no_gaps_once_everything_is_filed(self, db, patient, cardiology, storage):
        for doc_type in (DocumentType.ECG_REPORT, DocumentType.INSURANCE_CARD):
            document_service.store_document(
                db, patient_id=patient.id, filename=f"{doc_type.value}.pdf",
                content=doc_type.value.encode(), document_type=doc_type,
            )
        result = document_service.check_missing_documents(
            db, patient_id=patient.id, department_id=cardiology.id
        )
        assert result.missing == []
        assert set(result.present) >= {DocumentType.ECG_REPORT, DocumentType.INSURANCE_CARD}

    def test_duplicates_do_not_count_as_present(self, db, patient, cardiology, storage):
        """A re-uploaded file must not satisfy a requirement twice over."""
        content = b"one ecg"
        document_service.store_document(
            db, patient_id=patient.id, filename="ecg.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        document_service.store_document(
            db, patient_id=patient.id, filename="ecg_again.pdf",
            content=content, document_type=DocumentType.ECG_REPORT,
        )
        result = document_service.check_missing_documents(
            db, patient_id=patient.id, department_id=cardiology.id
        )
        assert result.present.count(DocumentType.ECG_REPORT) == 1

    def test_a_mis_seeded_requirement_does_not_break_the_check(self, db, patient, cardiology, storage):
        """One bad value must not take down the whole gap check."""
        cardiology.required_document_types = ["ecg_report", "not_a_real_type"]
        db.commit()
        result = document_service.check_missing_documents(
            db, patient_id=patient.id, department_id=cardiology.id
        )
        assert result.required == [DocumentType.ECG_REPORT]


class _FakeAIMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _FakeAIMessage(self.content)


class TestSummarizeDocument:
    """On-demand administrative summaries — never generated at upload time."""

    def test_no_extractable_text_gets_an_honest_placeholder(self, db, patient, storage):
        doc = document_service.store_document(
            db, patient_id=patient.id, filename="scan.jpg",
            content=b"\xff\xd8\xff\xe0 not really a jpeg but not text either",
            document_type=DocumentType.IMAGING_REPORT,
        )
        result = document_service.summarize_document(db, document_id=doc.id)
        assert result.summary == document_service.NO_EXTRACTABLE_TEXT_SUMMARY
        assert result.summary_generated_at is not None

    def test_txt_file_is_summarized_via_the_llm(self, db, patient, storage, monkeypatch):
        doc = document_service.store_document(
            db, patient_id=patient.id, filename="notes.txt",
            content=b"Lipid panel dated 2026-01-10, ordered by Dr. Rao at City Hospital.",
            document_type=DocumentType.BLOOD_REPORT,
        )
        fake_llm = _FakeLLM("A lipid panel from City Hospital, dated 10 Jan 2026.")
        monkeypatch.setattr("app.agents.llm.get_llm", lambda agent_name: fake_llm)

        result = document_service.summarize_document(db, document_id=doc.id, actor_label="staff")

        assert result.summary == "A lipid panel from City Hospital, dated 10 Jan 2026."
        assert fake_llm.calls, "the LLM should have been invoked once"

    def test_unsafe_llm_output_is_never_persisted(self, db, patient, storage, monkeypatch):
        """The summary call bypasses the agent graph's after_model safety hook, so it
        must run its own output scan — this is the regression test for that."""
        doc = document_service.store_document(
            db, patient_id=patient.id, filename="notes.txt",
            content=b"Some report text.", document_type=DocumentType.BLOOD_REPORT,
        )
        fake_llm = _FakeLLM("You have diabetes and should start taking metformin 500mg twice a day.")
        monkeypatch.setattr("app.agents.llm.get_llm", lambda agent_name: fake_llm)

        result = document_service.summarize_document(db, document_id=doc.id)

        assert "metformin" not in result.summary.lower()
        assert "diabetes" not in result.summary.lower()
