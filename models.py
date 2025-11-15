class Position:
    def __init__(self, ticker, direction, leverage, leverage_type, size, entry_price, volume):
        self.ticker = ticker
        self.direction = direction
        self.leverage = leverage
        self.leverage_type = leverage_type
        self.size = size
        self.entry_price = entry_price
        self.volume = volume
    
    def __eq__(self, other):
        if not isinstance(other, Position):
            return False
        return (self.ticker == other.ticker and
                self.direction == other.direction and
                self.leverage == other.leverage and
                self.leverage_type == other.leverage_type and
                self.size == other.size)


class Trade:
    def __init__(self, ticker, price, size, action):
        self.ticker = ticker
        self.price = price
        self.size = size
        self.action = action