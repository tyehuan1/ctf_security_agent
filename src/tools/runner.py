"""Tool runner: the only path from a model tool request to execution in the solver sandbox. PHASE 1 STUB.

Pattern from the triage agent's ToolRunner.execute(): reject unknown names and invalid arguments
(one correction, then failed), assign call IDs (T1, T2, ...), run with the controller-supplied
timeout, return {call_id, tool, status, output, error}. The controller caps output (head+tail) and
logs truncation; this runner must not change limits or target scope.
"""


class ToolRunner:
    def __init__(self, settings: dict, workspace):
        self.settings = settings
        self.workspace = workspace

    def execute(self, name: str, arguments, timeout_s: float) -> dict:
        raise NotImplementedError("Phase 1")
