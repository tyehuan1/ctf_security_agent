"""Solver tool definitions (OpenAI function format) and argument validation. PHASE 1 STUB.

Pattern from the triage agent's registry.py: tool_specs() builds the function list sent to the model,
validate_arguments() checks arguments against each spec's JSON Schema, and the model never invents
call IDs. No web, search, or answer-lookup tools belong here.
"""


def tool_specs(settings: dict) -> list[dict]:
    raise NotImplementedError("Phase 1")


def validate_arguments(name: str, args: dict, settings: dict) -> None:
    raise NotImplementedError("Phase 1")
