from utils import more_than_hour_ago


class TradeAction:
    def __init__(self, ticker, action):
        self.ticker = ticker
        self.action = action
    
    def __eq__(self, other):
        if not isinstance(other, TradeAction):
            return False
        return self.ticker == other.ticker and self.action == other.action
    
    def __hash__(self):
        return hash((self.ticker, self.action))


class PositionSnapshotKey:
    def __init__(self, ticker, direction, leverage, leverage_type):
        self.ticker = ticker
        self.direction = direction
        self.leverage = leverage
        self.leverage_type = leverage_type
    
    def __eq__(self, other):
        if not isinstance(other, PositionSnapshotKey):
            return False
        return (self.ticker == other.ticker and
                self.direction == other.direction and
                self.leverage == other.leverage and
                self.leverage_type == other.leverage_type)
    
    def __hash__(self):
        return hash((self.ticker, self.direction, self.leverage, self.leverage_type))


class PositionSnapshotValue:
    def __init__(self, size, volume):
        self.size = size
        self.volume = volume


class Position:
    def __init__(self, ticker, direction, leverage, leverage_type, size, entry_price, volume):
        self.ticker = ticker
        self.direction = direction
        self.leverage = leverage
        self.leverage_type = leverage_type
        self.size = size
        self.entry_price = entry_price
        self.volume = volume
        self.delta = 0
        self.is_new = False
    
    def __eq__(self, other):
        if not isinstance(other, Position):
            return False
        return (self.ticker == other.ticker and
                self.direction == other.direction and
                self.leverage == other.leverage and
                self.leverage_type == other.leverage_type and
                self.size == other.size)


class Trade:
    def __init__(self, ticker, price, size, action, timestamp):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.action = action
        self.timestamp = timestamp


class MessageId:
    def __init__(self, chat_id: int, message_id: int):
        self.chat_id = chat_id
        self.message_id = message_id


class Account:
    def __init__(self, tag=None):
        if tag is None or len(tag) == 0:
            self.tag = None
        else:
            self.tag = tag
        self.positions: list[Position] = None
        self.last_actions: dict[TradeAction, int] = {} # maps trade action to timestamp
        self.snapshot: dict[PositionSnapshotKey, PositionSnapshotValue] = {}
        self.closed_positions: list[str] = [] # list of tickers
        self.need_new_message = False
        self.last_message = ''
        self.last_trade = None
        self.message_ids: list[MessageId] = []
    
    def update(self, last_trade: Trade, new_positions: list[Position]):
        self.last_trade = last_trade
        action = TradeAction(last_trade.ticker, last_trade.action)
        time = last_trade.timestamp
        self.need_new_message = action not in self.last_actions or more_than_hour_ago(time, self.last_actions[action])
        self.__remove_old_actions(time)
        self.last_actions[action] = time
        if self.need_new_message:
            self.__make_snapshot()
        self.positions = new_positions
        self.__calculate_delta()
        self.__sort_positions()

    def __remove_old_actions(self, time: int):
        actions_to_delete = set()
        for a, t in self.last_actions.items():
            if more_than_hour_ago(time, t):
                actions_to_delete.add(a)
        for a in actions_to_delete:
            del self.last_actions[a]
    
    def __make_snapshot(self):
        self.snapshot = {}
        for pos in self.positions:
            self.snapshot[PositionSnapshotKey(
                ticker=pos.ticker,
                direction=pos.direction,
                leverage=pos.leverage,
                leverage_type=pos.leverage_type
            )] = PositionSnapshotValue(size=pos.size, volume=pos.volume)
    
    def __calculate_delta(self):
        local_snapshot = self.snapshot.copy()
        self.closed_positions = []
        for pos in self.positions:
            pos_snapshot_key = PositionSnapshotKey(
                ticker=pos.ticker,
                direction=pos.direction,
                leverage=pos.leverage,
                leverage_type=pos.leverage_type
            )
            if pos_snapshot_key in local_snapshot:
                if local_snapshot[pos_snapshot_key].size != pos.size:
                    pos.delta = pos.volume - local_snapshot[pos_snapshot_key].volume
                del local_snapshot[pos_snapshot_key]
            else:
                pos.is_new = True
        for pos_snapshot_key in local_snapshot.keys():
            self.closed_positions.append(pos_snapshot_key.ticker)
    
    def __sort_positions(self):
        self.positions.sort(key=lambda p: p.volume, reverse=True)