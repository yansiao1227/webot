"""隔离验证自动跟随状态机，不启动微信、模型或真实服务。"""
import sys
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supervisor as s
import autostart


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.services = Mock()
        self.services.__enter__ = Mock(return_value=self.services)
        self.services.__exit__ = Mock(return_value=False)
        self.services.paused.return_value = False
        self.services.configured.return_value = ({'account': 'self'}, 'h', 'm')
        self.services.present.return_value = {'hook': False, 'mcp': False, 'codex': False}
        self.services.health.return_value = True
        self.target = {'state': 'ready', 'pid': 10, 'created': 100, 'account': 'self'}
        self.probe = Mock(return_value=self.target)
        self.now = 0
        self.watcher = s.Supervisor(lambda: self.services, self.probe, lambda: self.now)

    def start(self):
        self.assertEqual(self.watcher.tick(), 'stabilizing')
        self.assertEqual(self.watcher.tick(), 'running')
        self.services.present.return_value = dict.fromkeys(['hook', 'mcp', 'codex'], True)

    def test_login_requires_two_identical_snapshots(self):
        self.probe.return_value = {'state': 'waiting_login'}
        self.assertEqual(self.watcher.tick(), 'waiting_login')
        self.services.start.assert_not_called()
        self.probe.return_value = self.target
        self.start()
        self.services.start.assert_called_once_with(self.target)
        self.assertEqual(self.watcher.tick(), 'running')
        self.services.start.assert_called_once()

    def test_crash_stops_then_new_instance_restarts(self):
        self.start()
        self.probe.return_value = {'state': 'waiting_wechat'}
        self.assertEqual(self.watcher.tick(), 'waiting_wechat')
        self.services.stop.assert_called_once()
        self.services.present.return_value = dict.fromkeys(['hook', 'mcp', 'codex'], False)
        self.probe.return_value = dict(self.target, created=200)
        self.assertEqual(self.watcher.tick(), 'stabilizing')
        self.assertEqual(self.watcher.tick(), 'running')
        self.assertEqual(self.services.start.call_count, 2)

    def test_pid_reuse_stops_old_stack(self):
        self.start()
        self.probe.return_value = dict(self.target, created=200)
        self.assertEqual(self.watcher.tick(), 'stabilizing')
        self.services.stop.assert_called_once()

    def test_wrong_account_stops_without_start(self):
        self.start()
        self.probe.return_value = dict(self.target, account='other')
        self.assertEqual(self.watcher.tick(), 'waiting_account')
        self.services.stop.assert_called_once()
        self.services.start.assert_called_once()

    def test_stop_timeout_blocks_replacement_and_backs_off(self):
        self.start()
        self.probe.return_value = dict(self.target, created=200)
        self.services.stop.side_effect = RuntimeError('timeout')
        self.assertEqual(self.watcher.tick(), 'retry_wait')
        self.watcher.tick()
        self.services.stop.assert_called_once()
        self.services.start.assert_called_once()
        self.now = 10
        self.watcher.tick()
        self.assertEqual(self.services.stop.call_count, 2)
        self.services.start.assert_called_once()

    def test_manual_pause_prevents_restart(self):
        self.services.paused.return_value = True
        self.assertEqual(self.watcher.tick(), 'paused')
        self.probe.assert_not_called()
        self.services.start.assert_not_called()

    def test_unhealthy_business_is_not_restarted(self):
        self.start()
        self.services.health.return_value = False
        self.assertEqual(self.watcher.tick(), 'degraded')
        self.services.stop.assert_not_called()
        self.services.start.assert_called_once()

    def test_probe_validation_failure_stops_existing_services(self):
        self.probe.side_effect = RuntimeError('version mismatch')
        self.assertEqual(self.watcher.tick(), 'retry_wait')
        self.services.stop.assert_called_once()
        self.services.start.assert_not_called()

    def test_missing_service_is_cleaned_before_restart(self):
        self.start()
        self.services.present.return_value['mcp'] = False
        self.assertEqual(self.watcher.tick(), 'running')
        self.services.stop.assert_called_once()
        self.assertEqual(self.services.start.call_count, 2)

    def test_autostart_uses_hidden_interpreter_and_absolute_entry(self):
        with patch.object(Path, 'is_file', return_value=True):
            value = autostart.command()
        self.assertIn('pythonw.exe', value)
        self.assertIn(str(Path(s.__file__).resolve()), value)
        self.assertTrue(value.endswith(' run'))

    def test_supervisor_lock_rejects_second_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = s.manager.StateLock(Path(directory))
            try:
                with self.assertRaises(RuntimeError):
                    s.manager.StateLock(Path(directory))
            finally:
                lock.close()

    def test_start_checks_hook_instance_before_launching_mcp(self):
        m = s.manager
        with patch.object(m, 'configuration', return_value=({}, 'h', 'm')), \
             patch.object(m, 'processes', return_value=[]), \
             patch.object(m, 'occupied', return_value=False), \
             patch.object(m, 'spawn') as spawn, patch.object(m, 'wait_ready'), \
             patch.object(m, 'request', return_value={'capture': {'native': {'pid': 11}}}), \
             patch.object(m, 'stop_one') as stop:
            with self.assertRaisesRegex(RuntimeError, '实例不一致'):
                m.start(target=self.target)
            self.assertEqual(spawn.call_count, 1)
            stop.assert_called_once_with('client_hook')

    def test_start_verifies_identity_before_any_launch(self):
        m = s.manager
        with patch.object(m, 'configuration', return_value=({}, 'h', 'm')), \
             patch.object(m, 'processes', return_value=[]), \
             patch.object(m, 'occupied', return_value=False), patch.object(m, 'spawn') as spawn:
            with self.assertRaises(RuntimeError):
                m.start(target=self.target, verify=Mock(side_effect=RuntimeError('changed')))
            spawn.assert_not_called()

    def test_start_accepts_real_ready_shape_and_rechecks_each_stage(self):
        m = s.manager
        with patch.object(m, 'configuration', return_value=({}, 'h', 'm')), \
             patch.object(m, 'processes', return_value=[]), \
             patch.object(m, 'occupied', return_value=False), \
             patch.object(m, 'spawn') as spawn, patch.object(m, 'wait_ready'), \
             patch.object(m, 'request', return_value={'capture': {'native': {
                 'type': 'ready', 'pid': 10, 'generation': 1, 'self_id': 'self'}}}):
            verify = Mock()
            m.start(target=self.target, verify=verify)
            self.assertEqual(spawn.call_count, 3)
            self.assertEqual(verify.call_count, 6)

    def test_manual_stop_pauses_before_stopping_and_start_resumes(self):
        m = s.manager
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(m, 'STATE', Path(directory)), \
             patch.object(m, 'stop_one') as stop, patch.object(m, 'start') as start:
            marker = Path(directory) / 'supervisor.pause'
            stop.side_effect = lambda name: self.assertTrue(marker.exists())
            with patch.object(sys, 'argv', ['manage.py', 'stop']):
                m.main()
            self.assertTrue(marker.exists())
            with patch.object(sys, 'argv', ['manage.py', 'start']):
                m.main()
            self.assertFalse(marker.exists())
            start.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
