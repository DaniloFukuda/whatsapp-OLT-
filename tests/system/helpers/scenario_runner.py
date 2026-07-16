from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ScenarioStep:
    description: str
    action: Callable
    expect: Callable | None = None


@dataclass
class ScenarioRunner:
    scenario_id: str
    title: str
    steps: list[ScenarioStep] = field(default_factory=list)

    def add_step(self, description: str, action: Callable, expect: Callable | None = None):
        self.steps.append(ScenarioStep(description, action, expect))
        return self

    def run(self):
        for index, step in enumerate(self.steps, 1):
            try:
                result = step.action()
                if step.expect:
                    step.expect(result)
            except Exception as exc:
                raise AssertionError(
                    f"{self.scenario_id} failed at step {index}: {step.description}. Difference: {exc}"
                ) from exc
