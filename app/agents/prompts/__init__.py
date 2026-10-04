"""System prompts — one per agent, each genuinely distinct.

`SAFETY_BOUNDARY` is shared verbatim by all six because it is a hard constraint rather
than a role description: it must read identically everywhere so no agent ends up with a
subtly weaker version of the rule. Everything else about each prompt differs — role,
procedure, failure handling, and what the agent is accountable for.
"""

from app.agents.prompts.appointment import APPOINTMENT_PROMPT
from app.agents.prompts.coordinator import COORDINATOR_PROMPT
from app.agents.prompts.document import DOCUMENT_PROMPT
from app.agents.prompts.followup import FOLLOWUP_PROMPT
from app.agents.prompts.routing import ROUTING_PROMPT
from app.agents.prompts.safety import SAFETY_PROMPT
from app.agents.prompts.shared import SAFETY_BOUNDARY, TOOL_DISCIPLINE

__all__ = [
    "SAFETY_BOUNDARY",
    "TOOL_DISCIPLINE",
    "COORDINATOR_PROMPT",
    "ROUTING_PROMPT",
    "APPOINTMENT_PROMPT",
    "DOCUMENT_PROMPT",
    "FOLLOWUP_PROMPT",
    "SAFETY_PROMPT",
]
