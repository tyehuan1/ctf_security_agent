"""Isolated solver: adaptive loop that proposes one action per turn. PHASE 1 STUB.

Runs inside the solver container. It receives only the controller's exported context (listed files,
sanitized description, local endpoint) and boolean verification results. It must never import
from src/controller, and it has no web or answer-lookup tools.

Implements the runner.Solver protocol: reset(workspace, context), request_next_action(observations),
execute(action, timeout_s).
"""


class Agent:
    def reset(self, workspace, context):
        raise NotImplementedError("Phase 1")

    def request_next_action(self, observations):
        raise NotImplementedError("Phase 1")

    def execute(self, action, timeout_s):
        raise NotImplementedError("Phase 1")
