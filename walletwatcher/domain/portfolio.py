"""Portfolio values shared by API adapters and presentation."""
from dataclasses import dataclass, field
from typing import Optional

from walletwatcher.domain.models import Position


@dataclass
class PersonalPosition:
    position: Position
    margin: float
    current_price: str
    roe: Optional[float]
    funding: Optional[float]
    take_profit: list[str] = field(default_factory=list)
    stop_loss: list[str] = field(default_factory=list)


@dataclass
class Portfolio:
    balance: float
    positions: list[PersonalPosition]
