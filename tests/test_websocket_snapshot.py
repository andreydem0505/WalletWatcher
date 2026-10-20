"""On-demand multi-DEX snapshots: protocol, cleanup and bounded failures."""
import json
import unittest
from unittest.mock import Mock, patch

import requests
import websocket

from walletwatcher.hyperliquid.client import DataFetchError, HyperliquidClient, WS_URL
from support import Clock, WALLET, OTHER
from test_personal_accounts import state, contexts, response


def frame(states=None, user=WALLET):
    return json.dumps({'channel': 'allDexsClearinghouseState', 'data': {
        'user': user, 'clearinghouseStates': states if states is not None else [['', state()]]}})


class WebsocketSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.session = Mock()
        self.session.post.return_value = response('disabled')
        self.client = HyperliquidClient(self.session, clock=self.clock,
                                       personal_session_factory=lambda: self.session)
        self.connection = Mock()
        self.connection.recv.return_value = frame()
        self.factory = self.enterContext(patch('walletwatcher.hyperliquid.client.websocket.WebSocket', return_value=self.connection))

    def test_initialization_and_monitoring_do_not_connect(self):
        self.session.post.return_value = response(state())
        self.client.fetch_open_positions(WALLET)
        self.factory.assert_not_called()

    def test_ack_is_not_mistaken_for_snapshot_and_only_public_address_is_sent(self):
        self.connection.recv.side_effect = [json.dumps({'channel': 'subscriptionResponse'}),
                                            json.dumps({'channel': 'pong'}), frame(user=WALLET.upper())]
        self.assertEqual(self.client._fetch_dex_states(WALLET), [('', state())])
        self.connection.connect.assert_called_once_with(WS_URL, timeout=5)
        self.assertEqual(json.loads(self.connection.send.call_args.args[0]), {
            'method': 'subscribe', 'subscription': {'type': 'allDexsClearinghouseState', 'user': WALLET}})
        self.assertEqual(self.connection.recv.call_count, 3)
        self.connection.close.assert_called_once_with(timeout=1)
        self.session.post.assert_not_called()

    def test_each_command_has_its_own_socket_closed_before_price_and_order_requests(self):
        sockets = [Mock(), Mock()]
        for connection in sockets:
            connection.recv.return_value = frame([['', state()], ['xyz', {
                'marginSummary': {'accountValue': '10'}, 'assetPositions': []}]])
        self.factory.side_effect = sockets
        replies = {'userAbstraction': 'disabled', 'metaAndAssetCtxs': contexts(), 'frontendOpenOrders': []}

        def post(*args, **kwargs):
            active = sockets[0] if not sockets[1].send.called else sockets[1]
            if kwargs['json']['type'] != 'userAbstraction':
                active.close.assert_called_once_with(timeout=1)
            self.assertNotIn(kwargs['json']['type'], ('perpDexs', 'clearinghouseState'))
            return response(replies[kwargs['json']['type']])

        self.session.post.side_effect = post
        self.assertEqual(self.client.fetch_portfolio(WALLET).balance, 87.43)
        self.assertEqual(self.client.fetch_portfolio(WALLET).balance, 87.43)
        self.assertEqual(self.factory.call_count, 2)
        for connection in sockets:
            connection.connect.assert_called_once()
            connection.close.assert_called_once_with(timeout=1)

    def test_invalid_frames_fail_without_position_requests_or_partial_portfolio(self):
        messages = ['broken json', '[]', '{}', frame(user=OTHER), frame(user=None),
            frame([]), frame({}), frame([['xyz', state()]]), frame([['', state()], ['', state()]]),
            frame([['', None]]), frame([['']]), frame(['not a pair']),
            frame([['', state()], [None, state()]]), json.dumps({'channel': 'error', 'data': 'unavailable'})]
        for message in messages:
            with self.subTest(message=message):
                self.connection.reset_mock()
                self.connection.recv.return_value = message
                with self.assertRaises(DataFetchError):
                    self.client.fetch_portfolio(WALLET)
                self.connection.close.assert_called_once_with(timeout=1)
                self.assertTrue(all(call.kwargs['json']['type'] == 'userAbstraction'
                                    for call in self.session.post.call_args_list))

    def test_transport_errors_at_any_stage_close_socket(self):
        for stage, error in [('connect', OSError('connect failed')),
                             ('send', websocket.WebSocketConnectionClosedException()),
                             ('recv', websocket.WebSocketTimeoutException()),
                             ('recv', websocket.WebSocketConnectionClosedException())]:
            with self.subTest(stage=stage, error=type(error).__name__):
                self.connection.reset_mock(side_effect=True)
                self.connection.recv.return_value = frame()
                getattr(self.connection, stage).side_effect = error
                with self.assertRaises(DataFetchError):
                    self.client.fetch_portfolio(WALLET)
                self.connection.close.assert_called_once_with(timeout=1)
                self.assertTrue(all(call.kwargs['json']['type'] == 'userAbstraction'
                                    for call in self.session.post.call_args_list))

    def test_clean_disconnect_before_snapshot_fails_and_closes(self):
        self.connection.recv.return_value = ''
        with self.assertRaises(DataFetchError):
            self.client._fetch_dex_states(WALLET)
        self.connection.close.assert_called_once_with(timeout=1)

    def test_ack_stream_cannot_extend_overall_deadline(self):
        def ack():
            self.clock.advance(10)
            return json.dumps({'channel': 'subscriptionResponse'})

        self.connection.recv.side_effect = ack
        with self.assertRaisesRegex(DataFetchError, 'timed out'):
            self.client._fetch_dex_states(WALLET)
        self.assertEqual(self.connection.recv.call_count, 2)
        self.assertEqual([call.args[0] for call in self.connection.settimeout.call_args_list], [15, 10])
        self.connection.close.assert_called_once_with(timeout=1)

    def test_socket_close_error_does_not_discard_valid_snapshot(self):
        self.connection.close.side_effect = OSError('already closed')
        heartbeat = Mock()
        self.client.set_heartbeat(heartbeat)
        self.assertEqual(self.client._fetch_dex_states(WALLET), [('', state())])
        self.assertGreaterEqual(heartbeat.call_count, 3)

    def test_connection_attempts_are_limited_even_when_setup_fails(self):
        self.connection.connect.side_effect = websocket.WebSocketTimeoutException()
        for _ in range(30):
            with self.assertRaises(DataFetchError):
                self.client._fetch_dex_states(WALLET)
        with self.assertRaisesRegex(DataFetchError, 'budget exhausted'):
            self.client._fetch_dex_states(WALLET)
        self.assertEqual(self.factory.call_count, 30)
        self.clock.advance(60)
        self.connection.connect.side_effect = None
        self.assertEqual(self.client._fetch_dex_states(WALLET), [('', state())])
        self.assertEqual(self.factory.call_count, 31)

    def test_http_failure_has_no_open_socket_and_does_not_return_partial_snapshot(self):
        self.session.post.side_effect = requests.Timeout()
        with self.assertRaises(DataFetchError):
            self.client.fetch_portfolio(WALLET)
        self.connection.close.assert_called_once_with(timeout=1)

    def test_snapshot_does_not_block_monitor_http_lock(self):
        # The monitor may be in an HTTP request when /my opens its socket.
        self.client.request_lock.acquire()
        try:
            self.assertEqual(self.client._fetch_dex_states(WALLET), [('', state())])
        finally:
            self.client.request_lock.release()
