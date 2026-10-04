"""Reading tool calls out of middleware requests.

These exist because of a live bug: both AuditMiddleware and ClinicalSafetyMiddleware
read `request.tool_call` with `getattr(..., default)`. LangChain's `ToolCall` is a
`TypedDict` — a plain dict at runtime — so attribute access always missed and always
returned the default, with no error raised.

The consequences were asymmetric. The audit trail merely became useless (every entry
recorded as `unknown_tool` with empty arguments). The safety middleware became a
*no-op*: its argument scan iterates `args.items()`, and an always-empty dict meant
layer 5 of the clinical boundary silently inspected nothing at all.

`getattr` with a fallback cannot fail, which is what made this invisible — so the
tests below assert on real dict-shaped tool calls rather than on mocks that would
happily answer either access style.
"""

from types import SimpleNamespace

import pytest

from app.agents.middleware.toolcall import tool_args, tool_call_id, tool_name


def make_request(tool_call):
    """A middleware request whose `tool_call` is whatever shape we want to test."""
    return SimpleNamespace(tool_call=tool_call, runtime=SimpleNamespace(context=None))


class TestDictShapedToolCalls:
    """The real runtime shape — LangChain ToolCall is a TypedDict."""

    def test_reads_the_name(self):
        request = make_request({"name": "book_appointment", "args": {"slot_id": 4}, "id": "c1"})
        assert tool_name(request) == "book_appointment"

    def test_reads_the_arguments(self):
        request = make_request({"name": "book_appointment", "args": {"slot_id": 4}, "id": "c1"})
        assert tool_args(request) == {"slot_id": 4}

    def test_reads_the_call_id(self):
        request = make_request({"name": "book_appointment", "args": {}, "id": "call_abc"})
        assert tool_call_id(request) == "call_abc"

    def test_name_is_never_silently_unknown_for_a_real_call(self):
        """The exact regression: a well-formed call must never read as unknown_tool."""
        request = make_request({"name": "cancel_appointment", "args": {}, "id": "c1"})
        assert tool_name(request) != "unknown_tool"


class TestObjectShapedToolCalls:
    """Accepted too, so a future LangChain promoting ToolCall to an object does not
    reintroduce the same bug in the opposite direction."""

    def test_reads_attributes(self):
        request = make_request(SimpleNamespace(name="find_slots", args={"dept": 2}, id="c9"))
        assert tool_name(request) == "find_slots"
        assert tool_args(request) == {"dept": 2}
        assert tool_call_id(request) == "c9"


class TestDegradedInput:
    def test_missing_tool_call_falls_back(self):
        request = SimpleNamespace(runtime=None)
        assert tool_name(request) == "unknown_tool"
        assert tool_args(request) == {}
        assert tool_call_id(request) == ""

    def test_explicit_none_args_becomes_an_empty_dict(self):
        request = make_request({"name": "t", "args": None, "id": "c1"})
        assert tool_args(request) == {}

    def test_non_dict_args_becomes_an_empty_dict(self):
        """Callers iterate `.items()`; a non-dict must not raise there."""
        request = make_request({"name": "t", "args": "not a dict", "id": "c1"})
        assert tool_args(request) == {}

    def test_missing_name_falls_back(self):
        assert tool_name(make_request({"args": {}, "id": "c1"})) == "unknown_tool"


class TestSafetyArgumentScanReceivesRealArguments:
    """The consequence test: layer 5 only works if the arguments actually arrive.

    Asserted against `scan_agent_output` — the function the middleware calls — so this
    fails if the extraction regresses, independently of middleware wiring.
    """

    def test_unsafe_argument_is_visible_to_the_scanner(self):
        from app.services import safety_service

        request = make_request({
            "name": "classify_and_store_document",
            "args": {"content": "You have a mild arrhythmia. I recommend taking aspirin."},
            "id": "c1",
        })
        args = tool_args(request)
        assert args, "arguments must be non-empty or the safety scan inspects nothing"
        assert safety_service.scan_agent_output(args["content"]).is_flagged

    def test_administrative_argument_passes(self):
        from app.services import safety_service

        request = make_request({
            "name": "classify_and_store_document",
            "args": {"content": "ECG report dated 10 July 2026, filed under Cardiology."},
            "id": "c1",
        })
        content = tool_args(request)["content"]
        assert not safety_service.scan_agent_output(content).is_flagged

    @pytest.mark.parametrize(
        "unsafe",
        [
            "You should take 500mg twice a day.",
            "Your results show elevated cholesterol.",
            "I recommend taking ibuprofen.",
        ],
    )
    def test_each_unsafe_argument_shape_is_caught(self, unsafe):
        from app.services import safety_service

        request = make_request({"name": "send_notification", "args": {"message": unsafe}, "id": "c"})
        assert safety_service.scan_agent_output(tool_args(request)["message"]).is_flagged
