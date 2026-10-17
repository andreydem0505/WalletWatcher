"""Lifecycle failures and flood waits, without real network calls or sleeps."""
import os
import subprocess
from unittest.mock import Mock, patch

from telebot import apihelper

import bot
from config import ConfigError
from models import Account
import supervisor
from support import ApplicationFixture, WALLET


class LifecycleTests(ApplicationFixture):
    def test_entrypoints_reject_invalid_configuration_before_starting(self):
        for module in (bot, supervisor):
            with self.subTest(module=module.__name__), \
                    patch.object(module, 'in_docker', return_value=True), \
                    patch.object(module, 'configure_logging'), \
                    patch('config.Config.from_env', side_effect=ConfigError('bad configuration')), \
                    patch.object(module.logger, 'error'):
                self.assertEqual(module.main(), 78)

    def test_bot_entrypoint_sets_timeouts_takes_lock_and_runs(self):
        with patch.object(bot, 'in_docker', return_value=True), \
                patch.object(bot, 'configure_logging'), \
                patch('config.Config.from_env', return_value=self.config), \
                patch.object(bot.signal, 'signal'), \
                patch.object(bot, 'Application') as application, \
                patch.object(apihelper, 'CONNECT_TIMEOUT'), \
                patch.object(apihelper, 'READ_TIMEOUT'), \
                patch.object(apihelper, 'RETRY_ON_ERROR'):
            self.assertEqual(bot.main(), 0)
            application.assert_called_once_with(self.config)
            application.return_value.run.assert_called_once_with()
            self.assertEqual(apihelper.CONNECT_TIMEOUT, 5)
            self.assertEqual(apihelper.READ_TIMEOUT, 30)
            self.assertFalse(apihelper.RETRY_ON_ERROR)
        self.assertTrue((self.root / 'bot.lock').is_file())

    def test_orphaned_bot_stops_workers_and_closes_resources(self):
        with patch('bot.WorkerSupervisor') as workers, \
                patch.dict(os.environ, {'WALLETWATCHER_PARENT_PID': str(os.getppid() + 1)}), \
                patch.object(self.app.stop, 'wait', return_value=False):
            workers.return_value.join.return_value = True
            with self.assertRaisesRegex(RuntimeError, 'Supervisor disappeared'):
                self.app.run()
        self.assertTrue(self.app.stop.is_set())
        workers.return_value.join.assert_called_once_with(5)
        self.client.close.assert_called_once_with()

    def test_stubborn_child_is_killed_after_termination_timeout(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('bot', 10), 0]
        supervisor.terminate(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual([call.kwargs['timeout'] for call in process.wait.call_args_list], [10, 5])

    def test_process_start_failure_retries_before_clean_exit(self):
        process = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch('supervisor.subprocess.Popen', side_effect=[OSError('cannot spawn'), process]) as spawn, \
                self.assertLogs('supervisor', level='ERROR'):
            self.assertEqual(supervisor.supervise(['bot'], stop, self.root / 'heartbeat'), 0)
        self.assertEqual(spawn.call_count, 2)
        stop.wait.assert_called_once_with(2)

    def test_monitor_skips_wallet_until_its_retry_deadline(self):
        self.app.accounts = {WALLET: Account(next_poll=self.clock() + 60)}
        with patch.object(self.app.stop, 'is_set', side_effect=[False, False, True]), \
                patch.object(self.app.stop, 'wait'), \
                patch('bot.time.monotonic', self.clock):
            self.app.monitor(Mock())
        self.client.fetch_open_positions.assert_not_called()


class PollingCooldownTests(ApplicationFixture):
    def advance_time(self, seconds):
        self.clock.advance(seconds)
        return self.app.stop.is_set()

    def stop_polling(self, **kwargs):
        self.app.stop.set()
        return []

    def test_persisted_flood_wait_pauses_polling_with_active_heartbeats(self):
        self.queue.defer_global(120)
        deadline = self.clock() + 120
        heartbeat = Mock()
        def fetch(**kwargs):
            self.assertGreaterEqual(self.clock(), deadline)
            return self.stop_polling(**kwargs)
        self.sender.get_updates.side_effect = fetch
        with patch('bot.time.time', self.clock), \
                patch.object(self.app.stop, 'wait', side_effect=self.advance_time):
            self.app.polling(heartbeat)
        self.sender.get_updates.assert_called_once()
        self.assertGreaterEqual(heartbeat.call_count, 120)

    def test_polling_429_persists_deadline_and_retries_after_margin(self):
        error = apihelper.ApiTelegramException('getUpdates', None,
            dict(error_code=429, description='flood wait', parameters=dict(retry_after=2)))
        started = self.clock()
        def fetch(**kwargs):
            if self.sender.get_updates.call_count == 1:
                raise error
            self.assertGreaterEqual(self.clock(), started + 3)
            return self.stop_polling(**kwargs)
        self.sender.get_updates.side_effect = fetch
        with patch('bot.time.time', self.clock), patch('bot.time.monotonic', self.clock), \
                patch.object(self.app.stop, 'wait', side_effect=self.advance_time), \
                self.assertLogs('bot', level='WARNING'):
            self.app.polling(Mock())
        self.assertEqual(self.sender.get_updates.call_count, 2)
        self.assertEqual(float(self.queue.get_metadata('telegram_flood_until')), started + 3)
