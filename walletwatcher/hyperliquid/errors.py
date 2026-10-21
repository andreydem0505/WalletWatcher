"""Safe API failures shared by transport, budgets and trading workers."""


class DataFetchError(Exception):
    def __init__(self, message, *, operation=None, reason='network', status_code=None,
                 retry_after=0, retryable=True, submitted=None):
        super().__init__(message)
        self.operation = operation
        self.reason = reason
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable
        self.submitted = submitted

    def diagnostic(self):
        # Never include response bodies, request payloads, URLs or exception text.
        reasons = {'budget': 'the bot request budget is busy',
                   'trading_priority': 'another automatic position close has priority',
                   'worker_failure': 'the autoclose worker was interrupted; supervised restart is pending',
                   'rate_limit': 'Hyperliquid rate limiting is active',
                   'timeout': 'the Hyperliquid request timed out',
                   'network': 'a network connection failed',
                   'http': 'Hyperliquid rejected the data request',
                   'invalid_response': 'Hyperliquid returned invalid JSON'}
        result = reasons.get(self.reason, 'the data request failed')
        if type(self.status_code) is int and 100 <= self.status_code <= 599:
            result += f' (HTTP {self.status_code})'
        if self.operation in ('userRole', 'meta', 'perpDexs', 'clearinghouseState',
                              'userFills', 'userFillsByTime', 'allMids', 'orderStatus', 'exchange'):
            result += f' [request: {self.operation}]'
        return result
