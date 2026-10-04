"""Data lineage: which doctor availability, documents, and department matches a
workflow run actually used — built from the same AuditEvent rows AuditMiddleware
already writes for every tool call, filtered to the ones that represent real data the
pipeline consulted."""

from app.services import audit_service


def _record_tool_call(db, *, run_id, tool, status="succeeded", args=None, result=None, error=None):
    audit_service.record(
        db,
        action=f"tool_call:{tool}",
        entity_type="agent_tool",
        entity_id=run_id,
        actor_label="appointment_agent",
        metadata={
            "tool": tool,
            "args": args or {},
            "result": result,
            "status": status,
            "error": error,
            "workflow_run_id": run_id,
        },
    )


class TestLineageFiltering:
    def test_a_lineage_tool_call_appears_with_its_step_label(self, db):
        _record_tool_call(
            db,
            run_id=1,
            tool="find_available_slots",
            args={"department_name": "Cardiology"},
            result="Open slots in Cardiology:\n- slot_id=1: Mon 03 Aug 2026, 09:00 with Dr. Meera Iyer",
        )

        lineage = audit_service.list_lineage_for_workflow(db, 1)

        assert len(lineage) == 1
        assert lineage[0].step == "Checked doctor availability"
        assert lineage[0].tool == "find_available_slots"
        assert "Dr. Meera Iyer" in lineage[0].detail

    def test_a_non_lineage_tool_call_is_excluded(self, db):
        """get_patient_record, save_workflow_state etc. are real tool calls too, but
        they aren't "what data was this decision based on" — they stay in the raw
        audit trail, not the lineage view."""
        _record_tool_call(db, run_id=2, tool="get_patient_record", result="Patient: Asha Menon")

        lineage = audit_service.list_lineage_for_workflow(db, 2)

        assert lineage == []

    def test_multiple_lineage_calls_are_returned_in_order(self, db):
        _record_tool_call(
            db, run_id=3, tool="lookup_departments", result="Active departments: Cardiology, ..."
        )
        _record_tool_call(db, run_id=3, tool="find_available_slots", result="Open slots in Cardiology: ...")
        _record_tool_call(
            db, run_id=3, tool="book_appointment", args={"slot_id": 1}, result="Booked appointment #9"
        )

        lineage = audit_service.list_lineage_for_workflow(db, 3)

        assert [entry.step for entry in lineage] == [
            "Looked up hospital departments",
            "Checked doctor availability",
            "Booked appointment",
        ]

    def test_a_failed_tool_call_shows_the_error_not_a_stale_result(self, db):
        _record_tool_call(
            db, run_id=4, tool="book_appointment", status="failed", error="That slot was just taken."
        )

        lineage = audit_service.list_lineage_for_workflow(db, 4)

        assert len(lineage) == 1
        assert "Failed" in lineage[0].detail
        assert "just taken" in lineage[0].detail

    def test_a_pre_existing_row_with_no_captured_result_falls_back_to_its_arguments(self, db):
        """Rows written before AuditMiddleware started capturing tool results won't
        have a "result" key at all — lineage must still degrade gracefully rather than
        showing "None" or crashing."""
        audit_service.record(
            db,
            action="tool_call:find_available_slots",
            entity_type="agent_tool",
            entity_id=5,
            actor_label="appointment_agent",
            metadata={
                "tool": "find_available_slots",
                "args": {"department_name": "Cardiology"},
                "status": "succeeded",
                "workflow_run_id": 5,
                # no "result" key — the pre-upgrade shape
            },
        )

        lineage = audit_service.list_lineage_for_workflow(db, 5)

        assert len(lineage) == 1
        assert "Cardiology" in lineage[0].detail

    def test_no_lineage_for_an_unknown_run_is_an_empty_list_not_an_error(self, db):
        assert audit_service.list_lineage_for_workflow(db, 999) == []
