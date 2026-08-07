"""任务进度追踪：为界面提供可展开的逐项任务清单。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StepState:
    id: str
    label: str
    state: str = "pending"  # pending | running | done | skipped | failed
    detail: str = ""
    frac: float = 0.0


class ProgressTracker:
    """记录每个步骤的状态，供界面渲染 checklist。"""

    def __init__(self, steps: list[tuple[str, str]]):
        self._steps = {sid: StepState(id=sid, label=label) for sid, label in steps}
        self.overall: float = 0.0
        self.message: str = ""

    def step(
        self,
        sid: str,
        state: str | None = None,
        detail: str | None = None,
        frac: float | None = None,
    ) -> None:
        s = self._steps[sid]
        if state:
            s.state = state
        if detail is not None:
            s.detail = detail
        if frac is not None:
            s.frac = max(0.0, min(float(frac), 1.0))

    def get(self, sid: str) -> StepState:
        return self._steps[sid]

    def snapshot(self) -> dict:
        return {
            "overall": self.overall,
            "message": self.message,
            "steps": {
                sid: {
                    "label": s.label,
                    "state": s.state,
                    "detail": s.detail,
                    "frac": s.frac,
                }
                for sid, s in self._steps.items()
            },
        }
