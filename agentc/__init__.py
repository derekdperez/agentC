"""agentC — an AI agent team orchestration framework.

The workflow engine orchestrates *agents* (configured CLI tools backed by an API
provider, model and system prompt) that perform *tasks* (an ordered series of
*actions*). Tasks may run ad-hoc, on a schedule, or in response to events —
including Linux filesystem events.
"""

__version__ = "0.1.0"

from agentc.math_utils import add

__all__ = ["__version__", "add"]
