"""Domain state; imports have no network or thread side effects."""
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from walletwatcher.domain.utils import more_than_hour_ago


@dataclass(frozen=True)
class TradeAction:
    ticker: str
    action: str


@dataclass(frozen=True)
class PositionSnapshotKey:
    ticker: str
    direction: str


@dataclass
class PositionSnapshotValue:
    size: float
    volume: float
    leverage: float


@dataclass
class Position:
    ticker: str
    direction: str
    leverage: float
    leverage_type: str
    size: float
    entry_price: Optional[str]
    volume: float = field(compare=False)
    pnl: float = field(compare=False)
    delta: float = field(default=0, compare=False)
    is_new: bool = field(default=False, compare=False)
    max_leverage: Optional[float] = field(default=None, compare=False)

    @property
    def key(self):
        # Leverage and margin mode can change without closing the position.
        return PositionSnapshotKey(self.ticker, self.direction)


@dataclass
class Trade:
    ticker: str
    price: str
    size: str
    action: str
    timestamp: int


@dataclass(frozen=True)
class MessageId:
    chat_id: int
    message_id: int


@dataclass
class Account:
    tag: Optional[str] = None
    positions: Optional[list[Position]] = None
    last_actions: dict[TradeAction, int] = field(default_factory=dict)
    snapshot: dict[PositionSnapshotKey, PositionSnapshotValue] = field(default_factory=dict)
    closed_positions: list[str] = field(default_factory=list)
    need_new_message: bool = False
    last_trade: Optional[Trade] = None
    generation: str = field(default_factory=lambda: uuid4().hex)
    last_updated: Optional[float] = None
    failures: int = 0
    next_poll: float = 0

    def update(self, last_trade: Trade, new_positions: list[Position]) -> None:
        action = TradeAction(last_trade.ticker, last_trade.action)
        previous = self.last_actions.get(action)
        self.need_new_message = previous is None or more_than_hour_ago(last_trade.timestamp, previous)
        self.last_actions = {
            key: time for key, time in self.last_actions.items()
            if not more_than_hour_ago(last_trade.timestamp, time)
        }
        self.last_actions[action] = last_trade.timestamp
        if self.need_new_message:
            self.generation = uuid4().hex
            self.snapshot = {pos.key: PositionSnapshotValue(pos.size, pos.volume, pos.leverage)
                             for pos in self.positions or []}
        self.last_trade = last_trade
        self.positions = sorted(new_positions, key=lambda pos: (-pos.volume, pos.ticker))
        remaining = self.snapshot.copy()
        for pos in self.positions:
            pos.delta = 0
            pos.is_new = pos.key not in remaining
            old = remaining.pop(pos.key, None)
            if old is not None and old.size != pos.size:
                pos.delta = pos.volume - old.volume
        self.closed_positions = sorted({key.ticker for key in remaining})
