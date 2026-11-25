"""Single source of truth for built-in prediction strategy metadata.

User-authored strategies live in the database and are exposed separately from
this code-defined registry. Registry entries always refer to an installed,
reviewed implementation.
"""

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class StrategyCapabilities:
    manual_run: bool = True
    scheduled_run: bool = False
    backtest: bool = False
    automatic_backtest: bool = False
    streaming: bool = False
    requires_ai_budget: bool = False
    editable_parameters: bool = False
    number_tuning: bool = False
    per_user_subscription: bool = True


@dataclass(frozen=True)
class StrategyDescriptor:
    key: str
    label: str
    icon: str
    emoji: str = "🎯"
    button_class: str = "btn-primary"
    display_order: int = 100
    kind: str = "builtin"
    output_schema_id: str = "number_selection"
    regions: tuple = ("hk", "macau")
    capabilities: StrategyCapabilities = StrategyCapabilities()
    enabled: bool = True


_BUILTIN_STRATEGIES = (
    StrategyDescriptor("zodiac_rebound", "生肖遗漏回补", "paw", "🐾", "btn-primary", 80,
                       output_schema_id="zodiac_groups",
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True)),
    StrategyDescriptor("hot", "热门预测", "fire", "🔥", "btn-danger", 40,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("cold", "冷门预测", "snowflake", "🧊", "btn-secondary", 50,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("trend", "走势预测", "chart-line", "📈", "btn-primary", 60,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("hybrid", "综合预测", "sliders-h", "♻️", "btn-success", 70,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("balanced", "均衡预测", "balance-scale", "⚖️", "btn-success", 30,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("markov", "马尔科夫预测", "project-diagram", "🔗", "btn-primary", 20,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("ml", "机器学习预测", "flask", "🧪", "btn-info", 10,
                       capabilities=StrategyCapabilities(scheduled_run=True, backtest=True,
                                                         automatic_backtest=True, number_tuning=True)),
    StrategyDescriptor("ai", "AI智能预测", "robot", "🤖", "btn-warning", 100, kind="ai_template",
                       capabilities=StrategyCapabilities(backtest=True, streaming=True,
                                                         requires_ai_budget=True)),
)


class StrategyCatalog:
    """Immutable metadata lookup used by server code and API serialization."""

    def __init__(self, strategies=_BUILTIN_STRATEGIES):
        self._strategies = {}
        for strategy in strategies:
            if strategy.key in self._strategies:
                raise ValueError(f"duplicate strategy key: {strategy.key}")
            self._strategies[strategy.key] = strategy

    def get(self, key: str) -> Optional[StrategyDescriptor]:
        return self._strategies.get(str(key or "").strip())

    def all(self, include_disabled=False):
        return tuple(
            item for item in self._strategies.values()
            if include_disabled or item.enabled
        )

    def keys(self, capability=None, kind=None):
        items = self.all()
        if kind:
            items = tuple(item for item in items if item.kind == kind)
        if capability:
            items = tuple(
                item for item in items
                if bool(getattr(item.capabilities, capability, False))
            )
        return tuple(item.key for item in items)

    def serialize(self, capability=None, kind=None):
        items = self.all()
        if kind:
            items = tuple(item for item in items if item.kind == kind)
        if capability:
            items = tuple(
                item for item in items
                if bool(getattr(item.capabilities, capability, False))
            )
        values = []
        for item in items:
            value = asdict(item)
            value["regions"] = list(item.regions)
            value["id"] = item.key
            value["display_name"] = item.label
            value["execution_adapter_id"] = "legacy-ai-stream" if item.key == "ai" else f"builtin:{item.key}"
            value["default_config_version"] = 1
            value["requires_external_data"] = item.key == "ai"
            value["parameter_schema"] = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "window": {"type": "integer", "minimum": 1, "maximum": 240},
                    "pool": {"type": "integer", "minimum": 8, "maximum": 24},
                    "special_pool": {"type": "integer", "minimum": 6, "maximum": 14},
                    "trend_window": {"type": "integer", "minimum": 1, "maximum": 60},
                } if item.capabilities.number_tuning else {},
            }
            if item.key == "zodiac_rebound":
                value["parameter_schema"]["properties"] = {
                    "window": {"type": "integer", "minimum": 10, "maximum": 240, "default": 20},
                    "parity_streak_threshold": {"type": "integer", "minimum": 2, "maximum": 20, "default": 5},
                }
            value["capabilities"]["clonable"] = item.key != "ai"
            value["capabilities"]["editable_parameters"] = item.capabilities.number_tuning or item.key == "zodiac_rebound"
            values.append(value)
        return values


strategy_catalog = StrategyCatalog()


def get_strategy(key: str) -> Optional[StrategyDescriptor]:
    return strategy_catalog.get(key)


def get_strategy_keys(capability=None, kind=None):
    return list(strategy_catalog.keys(capability=capability, kind=kind))


def get_strategy_catalog(capability=None, kind=None):
    return strategy_catalog.serialize(capability=capability, kind=kind)


def get_strategy_labels():
    return {item.key: item.label for item in strategy_catalog.all()}


def get_strategy_icons():
    return {item.key: item.icon for item in strategy_catalog.all()}


def get_strategy_menu_meta(capability=None):
    return [
        {
            "key": item.key,
            "label": item.label[:-2] if item.label.endswith("预测") else item.label,
            "icon": item.emoji,
        }
        for item in strategy_catalog.all()
        if capability is None or bool(getattr(item.capabilities, capability, False))
    ]
