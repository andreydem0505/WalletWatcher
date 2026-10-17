import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bot
from config import Config, ConfigError
from healthcheck import healthy
import supervisor


class ContainerTests(unittest.TestCase):
    def test_host_launch_is_rejected_before_configuration_or_network(self):
        for module in (bot, supervisor):
            with self.subTest(module=module.__name__), \
                    patch.object(module, 'in_docker', return_value=False), \
                    patch.object(module, 'configure_logging'), \
                    patch.object(module.logger, 'error'), \
                    patch('config.Config.from_env') as load:
                self.assertEqual(module.main(), 78)
                load.assert_not_called()

    def test_persistent_paths_use_environment_without_current_directory(self):
        environment = dict(BOT_TOKEN='12345:test', CHAT_IDS='1', ADMIN_ID='1',
                           WALLETWATCHER_DATA_DIR='/persistent')
        with patch('config.load_dotenv'), patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
            self.assertEqual(config.wallets_file, Path('/persistent/wallets.txt'))
            self.assertEqual(config.database_file, Path('/persistent/state.sqlite3'))
            with patch.dict(os.environ, {'WALLETWATCHER_DATA_DIR': 'relative'}):
                with self.assertRaises(ConfigError):
                    Config.from_env()

    def test_health_probe_handles_stale_missing_or_invalid_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / 'heartbeat'
            self.assertFalse(healthy(heartbeat, 180, now=1000))
            heartbeat.touch()
            os.utime(heartbeat, (1000, 1000))
            self.assertTrue(healthy(heartbeat, 180, now=1001))
            self.assertTrue(healthy(heartbeat, 180, now=1269))
            self.assertFalse(healthy(heartbeat, 180, now=1271))
            self.assertFalse(healthy(heartbeat, 180, now=999))
            for invalid in ('nan', 'inf', 'bad', 0, None):
                with self.subTest(timeout=invalid):
                    self.assertFalse(healthy(heartbeat, invalid, now=1000))

    def test_supervisor_uses_volume_and_removes_heartbeat_after_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / 'data'
            heartbeat = root / 'heartbeat'
            config = Config('12345:test', (1,), 1, 'TEST',
                            wallets_file=data / 'wallets.txt', database_file=data / 'state.sqlite3')

            def stopped(command, stop, path, timeout):
                self.assertEqual(path, heartbeat)
                self.assertTrue((data / 'supervisor.lock').is_file())
                path.touch()
                return 0

            with patch.object(supervisor, 'in_docker', return_value=True), \
                    patch.object(supervisor, 'configure_logging'), \
                    patch('config.Config.from_env', return_value=config), \
                    patch.object(supervisor.signal, 'signal'), \
                    patch.dict(os.environ, WALLETWATCHER_HEARTBEAT=str(heartbeat)), \
                    patch.object(supervisor, 'supervise', side_effect=stopped):
                self.assertEqual(supervisor.main(), 0)
            self.assertFalse(heartbeat.exists())


if __name__ == '__main__':
    unittest.main()
