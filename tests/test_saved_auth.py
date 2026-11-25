# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json, os, subprocess, sys, tempfile, unittest
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from saver.engine import Engine
from saver.pikpak import PikpakGateway
from saver.store import Store
from saver.web import _account

class Gateway:

    def __init__(self):
        self.status_calls = 0
        self.login_calls = 0
        self.connected = True
        self.identity = 'same-account'
        self.error = False

    def auth_status(self):
        self.status_calls += 1
        if self.error:
            raise RuntimeError('network unavailable')
        return {'logged_in': self.connected, 'user_id': self.identity if self.connected else '', 'name': 'saved account'}

    def auth_start(self):
        self.login_calls += 1
        return {'device_code': 'private-fixture-code', 'user_code': 'TEST-1234', 'verification_uri': 'https://example.invalid/device', 'expires_in': 300, 'interval': 2}

    def close(self):
        pass

    def cancel(self):
        pass

class EngineTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'jobs.sqlite3')
        self.gateway = Gateway()
        self.engine = Engine(self.store, SimpleNamespace(network=SimpleNamespace()), self.gateway, self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_starts_checking_saved_session(self):
        self.assertEqual(self.engine.account()['status'], 'checking')

    def test_repeated_connect_reuses_session_without_device_grants(self):
        for _ in range(3):
            self.assertEqual(self.engine.start_auth()['status'], 'connected')
        restarted = Engine(self.store, SimpleNamespace(network=SimpleNamespace()), self.gateway, self.path)
        self.assertEqual(restarted.start_auth()['status'], 'connected')
        self.assertEqual(self.gateway.login_calls, 0)

    def test_network_retry_never_requests_device_grant(self):
        self.gateway.error = True
        result = self.engine.start_auth()
        self.assertEqual(result['status'], 'error')
        self.assertEqual(self.gateway.login_calls, 0)

    def test_restored_session_dismisses_obsolete_device_prompt(self):
        self.engine._device = {'expires': float('inf'), 'next_poll': float('inf')}
        self.engine._public_update(device={'user_code': 'OLD-CODE'})
        result = self.engine.start_auth()
        self.assertIsNone(result['device'])
        self.assertIsNone(self.engine._device)
        self.assertEqual(self.gateway.login_calls, 0)

    def test_temporary_error_keeps_remembered_identity_but_blocks_work(self):
        self.engine._refresh_account()
        self.gateway.error = True
        self.engine._refresh_account(force=True)
        state = self.engine.account()
        self.assertTrue(state['connected'])
        self.assertEqual(state['status'], 'error')
        self.assertEqual(state['name'], 'saved account')
        self.assertFalse(self.engine._identity_ok)

    def test_failed_check_automatically_retries_after_ten_seconds(self):
        with patch('saver.engine.time.time', return_value=1000):
            self.engine._refresh_account()
        self.gateway.error = True
        with patch('saver.engine.time.time', return_value=1061):
            self.engine._refresh_account()
        with patch('saver.engine.time.time', return_value=1065):
            self.engine._refresh_account()
        self.assertEqual(self.gateway.status_calls, 2)
        self.gateway.error = False
        with patch('saver.engine.time.time', return_value=1072):
            self.engine._refresh_account()
        self.assertEqual(self.gateway.status_calls, 3)
        self.assertEqual(self.engine.account()['status'], 'connected')
        self.assertEqual(self.gateway.login_calls, 0)

    def test_only_confirmed_disconnected_state_starts_device_flow(self):
        self.gateway.connected = False
        state = self.engine.start_auth()
        self.assertEqual(self.gateway.login_calls, 1)
        self.assertEqual(state['status'], 'disconnected')
        self.assertEqual(state['device']['user_code'], 'TEST-1234')

    def test_account_mismatch_never_reauthorizes_or_submits(self):
        self.store.private_set('account_id', 'original-account')
        state = self.engine.start_auth()
        self.assertEqual(state['status'], 'error')
        self.assertFalse(self.engine._identity_ok)
        self.assertEqual(self.gateway.login_calls, 0)
        self.gateway.identity = 'original-account'
        self.assertEqual(self.engine.start_auth()['status'], 'connected')

    def test_saved_session_is_checked_before_source_discovery(self):

        def discover():
            self.assertEqual(self.engine.account()['status'], 'connected')
        with patch.object(self.engine, '_discover', side_effect=discover):
            self.engine.tick()
        self.assertEqual(self.gateway.status_calls, 1)

    def test_long_discovery_revalidates_identity_before_remote_work(self):
        now = [1000]

        def discover():
            now[0] += 61
            self.gateway.identity = 'other-account'
        with patch('saver.engine.time.time', side_effect=lambda: now[0]), patch.object(self.engine, '_discover', side_effect=discover):
            self.engine.tick()
        self.assertEqual(self.gateway.status_calls, 2)
        self.assertFalse(self.engine._identity_ok)
        self.assertEqual(self.engine.account()['status'], 'error')

    def test_public_state_omits_private_device_code(self):
        result = _account({'status': 'error', 'device': {'device_code': 'private-fixture-code', 'user_code': 'public-fixture-code'}})
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('private-fixture-code', json.dumps(result))

class FakeProc:

    def __init__(self, out, err, code):
        self.out = out
        self.err = err
        self.returncode = code

    def communicate(self, timeout=None):
        return (self.out, self.err)

class GatewayTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.gateway = PikpakGateway('/bin/false', self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def status(self, out, err='', code=0):
        with patch('saver.pikpak.subprocess.Popen', return_value=FakeProc(out, err, code)):
            return self.gateway.auth_status()

    def test_auxiliary_warning_does_not_discard_valid_login(self):
        r = self.status('VIP Error: temporary request failure\n' + json.dumps({'logged_in': True, 'user_id': 'same-account'}))
        self.assertTrue(r['logged_in'])

    def test_explicit_expiration_in_stderr_requests_reconnect(self):
        self.assertEqual(self.status('', json.dumps({'error': {'code': 'TOKEN_REFRESH_FAILED'}}), 1), {'logged_in': False})

    def test_explicit_expiration_in_stdout_requests_reconnect(self):
        self.assertEqual(self.status(json.dumps({'error': {'code': 'unauthenticated'}}), '', 1), {'logged_in': False})

    def test_network_failure_is_not_a_logout(self):
        with self.assertRaises(RuntimeError):
            self.status('', json.dumps({'error': {'code': 'NETWORK_ERROR'}}), 1)

    def test_malformed_reply_is_not_a_logout(self):
        for output in ('not json', '{}', '{"error":{"code":"NETWORK_ERROR"}}'):
            with self.assertRaises(RuntimeError):
                self.status(output)

    def test_valid_login_wins_over_auxiliary_error(self):
        self.assertTrue(self.status('{"logged_in":true,"user_id":"same-account"}', '{"error":{"code":"ACCESS_DENIED"}}')['logged_in'])

    def test_transfer_commands_keep_strict_uncertain_result_handling(self):
        with patch('saver.pikpak.subprocess.Popen', return_value=FakeProc('warning\n{"file_id":"fixture"}', '', 0)):
            with self.assertRaises(RuntimeError):
                self.gateway._command(['upload', 'fixture'])
if __name__ == '__main__':
    unittest.main(verbosity=2)
