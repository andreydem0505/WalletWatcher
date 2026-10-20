import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from walletwatcher import app as bot
from walletwatcher.config import Config, ConfigError, ROOT
from walletwatcher.infrastructure.healthcheck import healthy
from walletwatcher.infrastructure import supervisor
from walletwatcher.storage.wallets import WALLETS_FILE


class ContainerTests(unittest.TestCase):
    def test_package_entrypoints_import_and_reject_host_launch_in_real_child_processes(self):
        for module in ('walletwatcher.app', 'walletwatcher.infrastructure.supervisor'):
            code = (
                'import runpy\n'
                'from walletwatcher.infrastructure import runtime\n'
                'runtime.in_docker = lambda: False\n'
                f'runpy.run_module({module!r}, run_name="__main__")\n'
            )
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertIn('must run in Docker', result.stderr)

    def test_package_move_preserves_project_config_and_default_data_paths(self):
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(ROOT, expected)
        self.assertEqual(supervisor.ROOT, expected)
        config = Config('12345:test', (1,), 1, 'TEST')
        self.assertEqual(config.wallets_file, expected / 'wallets.txt')
        self.assertEqual(config.database_file, expected / 'state.sqlite3')
        self.assertEqual(WALLETS_FILE, expected / 'wallets.txt')
        environment = dict(BOT_TOKEN='12345:test', CHAT_IDS='1', ADMIN_ID='1')
        with patch.dict(os.environ, environment, clear=True), \
                patch('walletwatcher.config.load_dotenv') as load:
            Config.from_env()
            load.assert_called_once_with(expected / '.env')

    def test_host_launch_is_rejected_before_configuration_or_network(self):
        for module in (bot, supervisor):
            with self.subTest(module=module.__name__), \
                    patch.object(module, 'in_docker', return_value=False), \
                    patch.object(module, 'configure_logging'), \
                    patch.object(module.logger, 'error'), \
                    patch('walletwatcher.config.Config.from_env') as load:
                self.assertEqual(module.main(), 78)
                load.assert_not_called()

    def test_persistent_paths_use_environment_without_current_directory(self):
        environment = dict(BOT_TOKEN='12345:test', CHAT_IDS='1', ADMIN_ID='1',
                           WALLETWATCHER_DATA_DIR='/persistent')
        with patch('walletwatcher.config.load_dotenv'), patch.dict(os.environ, environment, clear=True):
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
                self.assertEqual(command, [sys.executable, '-m', 'walletwatcher.app'])
                self.assertEqual(path, heartbeat)
                self.assertTrue((data / 'supervisor.lock').is_file())
                path.touch()
                return 0

            with patch.object(supervisor, 'in_docker', return_value=True), \
                    patch.object(supervisor, 'configure_logging'), \
                    patch('walletwatcher.config.Config.from_env', return_value=config), \
                    patch.object(supervisor.signal, 'signal'), \
                    patch.dict(os.environ, WALLETWATCHER_HEARTBEAT=str(heartbeat)), \
                    patch.object(supervisor, 'supervise', side_effect=stopped):
                self.assertEqual(supervisor.main(), 0)
            self.assertFalse(heartbeat.exists())


if __name__ == '__main__':
    unittest.main()
