"""Execution contracts for registered prediction runners."""

from dataclasses import dataclass
from typing import Callable, Optional

from strategy_platform.catalog import get_strategy


@dataclass(frozen=True)
class ExecutionContext:
    strategy_key: str
    data: object
    region: str
    variation_key: Optional[str] = None
    target_period: Optional[str] = None
    spec: Optional[dict] = None
    number_to_zodiac: Optional[dict] = None
    source_snapshot: Optional[dict] = None
    backtest: bool = False
    target_date: Optional[str] = None


class RunnerRegistry:
    """Maps catalog entries to installed, reviewed execution functions."""

    def __init__(self):
        self._runners = {}
        self._kind_runners = {}

    def register(self, strategy_key: str, runner: Callable[[ExecutionContext], dict]):
        if get_strategy(strategy_key) is None:
            raise ValueError(f"strategy is not present in catalog: {strategy_key}")
        if strategy_key in self._runners:
            raise ValueError(f"runner already registered: {strategy_key}")
        self._runners[strategy_key] = runner

    def execute(self, context: ExecutionContext):
        strategy = get_strategy(context.strategy_key)
        if strategy is None or not strategy.enabled:
            raise ValueError(f"strategy is unavailable: {context.strategy_key}")
        if context.region not in strategy.regions:
            raise ValueError(f"strategy {context.strategy_key} does not support region {context.region}")
        runner = self._runners.get(context.strategy_key)
        if runner is None:
            raise ValueError(f"strategy has no installed runner: {context.strategy_key}")
        return runner(context)

    def register_kind(self, kind: str, runner: Callable[[ExecutionContext], dict]):
        if not kind or kind in self._kind_runners:
            raise ValueError(f"strategy adapter kind already registered: {kind}")
        self._kind_runners[kind] = runner

    def execute_kind(self, kind: str, context: ExecutionContext):
        runner = self._kind_runners.get(kind)
        if runner is None:
            raise ValueError(f"strategy kind has no installed adapter: {kind}")
        return runner(context)


strategy_runner_registry = RunnerRegistry()
