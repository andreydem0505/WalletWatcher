"""Additional delivery error paths and persistence guarantees."""
from types import SimpleNamespace

from telebot import apihelper

from support import ApplicationFixture, WALLET


class DeliveryBehaviorTests(ApplicationFixture):
    def error(self, code, description='test error'):
        return apihelper.ApiTelegramException('test', None,
            dict(error_code=code, description=description, parameters={}))

    def test_bad_request_retries_after_one_hour(self):
        self.sender.send_message.side_effect = self.error(400)
        self.queue.enqueue(1, 'alert')
        with self.assertLogs('walletwatcher.storage.delivery', level='WARNING'):
            self.assertTrue(self.queue.deliver_once())
        self.assertEqual(self.jobs()[0]['due'], self.clock() + 3600)

    def test_invalid_token_defers_all_recipients_for_five_minutes(self):
        self.sender.send_message.side_effect = self.error(401)
        self.queue.enqueue(1, 'alert')
        with self.assertLogs('walletwatcher.storage.delivery', level='ERROR'):
            self.assertTrue(self.queue.deliver_once())
        self.assertEqual(self.jobs()[0]['due'], self.clock() + 300)
        self.queue.enqueue(2, 'another chat')
        self.assertFalse(self.queue.deliver_once())

    def test_network_backoff_is_capped_and_preserved_when_text_changes(self):
        self.sender.send_message.side_effect = ConnectionError('offline')
        self.queue.enqueue(1, 'alert', key='persistent')
        for attempt in range(1, 11):
            with self.assertLogs('walletwatcher.storage.delivery', level='WARNING'):
                self.assertTrue(self.queue.deliver_once())
            job = self.queue.db.execute('SELECT * FROM outbox').fetchone()
            delay = min(300, 2 ** attempt)
            self.assertEqual(job['due'], self.clock() + delay)
            self.queue.enqueue(1, f'updated {attempt}', key='persistent')
            self.assertFalse(self.queue.deliver_once())
            self.clock.advance(max(delay, self.config.chat_interval))
        self.sender.send_message.side_effect = None
        self.assertTrue(self.queue.deliver_once())
        self.assertEqual(self.queue.pending_count(), 0)

    def test_deleted_wallet_during_send_does_not_leave_receipt(self):
        key = f'wallet:{WALLET}:1'
        self.queue.enqueue(1, 'alert', key=key, generation='one')
        def remove(**kwargs):
            self.queue.forget_wallet(WALLET)
            return SimpleNamespace(message_id=7)
        self.sender.send_message.side_effect = remove
        self.assertTrue(self.queue.deliver_once())
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM receipts').fetchone()[0], 0)

    def test_pruning_removed_chats_drops_pending_command_replies(self):
        self.queue.enqueue(99, 'old reply')
        self.queue.enqueue(1, 'allowed reply')
        self.queue.prune([], [1], [1, 2])
        self.assertEqual([job['chat'] for job in self.jobs()], [1])

    def test_empty_messages_do_not_consume_outbox_capacity(self):
        self.queue.enqueue(1, '')
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertFalse(self.queue.deliver_once())
        self.sender.send_message.assert_not_called()
