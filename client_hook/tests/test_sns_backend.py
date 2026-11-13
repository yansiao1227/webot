"""朋友圈后端纯 mock 回归；不启动服务、不访问微信、DLL 或账号数据库。"""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.backend import NativeBackend
from api.errors import ApiError


ACCOUNT = 'wxid_test_self'
AUTHOR = 'wxid_test_author'
POST_ID = '18446744073709551615'
PINS = json.dumps({'dll_sha256': 'a' * 64})


def native_result(**updates):
    value = {'state': 2, 'submitted': 1, 'completed': 1, 'cleaned': 1,
             'error': 0, 'thread_error': 0, 'calls': 1, 'status0': 0, 'status1': 0,
             'server_post_id': '12345678901234567890'}
    value.update(updates)
    return value


def params_for(action):
    if action == 'publish':
        return {'text': '纯 mock 发布正文', 'visibility': 'public'}
    params = {'post_id': POST_ID, 'author_id': AUTHOR}
    if action == 'comment':
        params['text'] = '纯 mock 评论正文'
    return params


@contextmanager
def mocked_execution(result, *, capabilities=None):
    backend = NativeBackend()
    backend.account = ACCOUNT
    backend.profile = Mock(return_value={'wxid': ACCOUNT})
    backend.sns_capabilities = Mock(return_value=capabilities or {
        action: True for action in ('like', 'unlike', 'comment', 'publish')})
    backend.sns_validate = Mock()
    backend.stopped = Mock()
    backend.stopped.wait.return_value = False
    # 连 pins 文件都不读磁盘；任何漏出的 Win() 都令测试立即失败。
    with patch('pathlib.Path.read_text', return_value=PINS) as read_text, \
            patch('hook_runtime.sns_rpc.Win', side_effect=AssertionError('禁止接触真实微信')), \
            patch('hook_runtime.sns_rpc.invoke', side_effect=[native_result(), result]) as invoke:
        yield backend, invoke, read_text


class SnsBackendTests(unittest.TestCase):
    def test_publish_requires_build_capability_before_profile_pins_and_rpc(self):
        backend = NativeBackend()
        backend.profile = Mock(side_effect=AssertionError('不得读取登录态'))
        backend.sns_capabilities = Mock(return_value={'publish': False})
        with patch('pathlib.Path.read_text') as read, patch('hook_runtime.sns_rpc.invoke') as invoke:
            with self.assertRaises(ApiError) as caught:
                backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'blocked_publish')
        self.assertEqual(caught.exception.code, 'sns_native_unavailable')
        self.assertEqual(caught.exception.status, 503)
        backend.profile.assert_not_called()
        backend.sns_capabilities.assert_called_once()
        read.assert_not_called()
        invoke.assert_not_called()

    def test_diagnostics_are_persisted_before_submit_without_body(self):
        with mocked_execution(native_result()) as (backend, invoke, _):
            records = []
            backend.store = Mock()
            backend.store.set_meta.side_effect = lambda key, value: records.append(json.loads(json.dumps(value)))
            result = backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'trace_request')
            stages = [r['events'][-1]['stage'] for r in records]
            self.assertEqual(stages, ['probe_begin', 'probe_result', 'submission_begin', 'submission_result'])
            self.assertEqual(records[-1]['submission_result']['calls'], 1)
            self.assertNotIn(params_for('publish')['text'], json.dumps(records, ensure_ascii=False))
            self.assertEqual(result['state'], 'confirmed')
            self.assertEqual(invoke.call_count, 2)

    def test_diagnostic_storage_failure_prevents_any_native_call(self):
        with mocked_execution(native_result()) as (backend, invoke, _):
            backend.store = Mock()
            backend.store.set_meta.side_effect = OSError('storage unavailable')
            with self.assertRaises(OSError):
                backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'trace_failure')
            invoke.assert_not_called()

    def test_logout_query_cannot_erase_submitted_observation_or_retry(self):
        submitted = native_result(state=1, completed=0, cleaned=0)
        rejected = native_result(state=0, submitted=0, completed=0, cleaned=0, calls=0,
                                 thread_error=13, server_post_id='0')
        for failure in (rejected, RuntimeError('query failed')):
            with self.subTest(failure=type(failure).__name__), mocked_execution(submitted) as (backend, invoke, _):
                invoke.side_effect = [native_result(), submitted, failure]
                result = backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'stable_logout')
                self.assertEqual(result['state'], 'unknown')
                self.assertEqual(result['native_observations'][0]['submitted'], 1)
                self.assertEqual(result['native_observations'][0]['calls'], 1)
                self.assertEqual(invoke.call_count, 3)  # probe + 一次提交 + 一次查询
                self.assertEqual(invoke.call_args.args[-1], 1)

    def test_action_mapping_and_complete_confirmation(self):
        for action, native_action in (('unlike', 1), ('like', 2), ('comment', 3), ('publish', 4)):
            with self.subTest(action=action), mocked_execution(native_result()) as (backend, invoke, read):
                params = params_for(action)
                result = backend.sns_execute(ACCOUNT, action, params, 'stable_request_01')
                self.assertEqual(result['state'], 'confirmed')
                self.assertTrue(result['local_accepted'])
                self.assertTrue(result['server_confirmed'])
                self.assertEqual(invoke.call_count, 2)
                probe, submission = invoke.call_args_list
                self.assertEqual(probe.args[0], ACCOUNT)
                self.assertTrue(probe.args[1].startswith('sns_probe_'))
                self.assertEqual(probe.args[2:], (0, 2))
                self.assertEqual(submission.args[0], ACCOUNT)
                self.assertEqual(submission.args[2:], (0 if action == 'publish' else int(POST_ID), 0))
                self.assertEqual(submission.kwargs, {
                    'action': native_action, 'author': params.get('author_id', ''),
                    'body': params.get('text', '')})
                backend.sns_validate.assert_called_once_with(ACCOUNT, action, params)
                read.assert_called_once()
                self.assertEqual(result['native_request_id'], submission.args[1])
                self.assertEqual(result['post_id'], '12345678901234567890' if action == 'publish' else POST_ID)

    def test_incomplete_or_error_result_remains_unknown_without_local_acceptance(self):
        invalid_results = (
            {'submitted': 0}, {'completed': 0}, {'cleaned': 0}, {'state': 4}, {'calls': 2},
            {'calls': 0}, {'error': 5}, {'thread_error': 5},
            {'status0': -110}, {'status1': -1}, {'status0': 0, 'status1': -7},
        )
        for invalid in invalid_results:
            with self.subTest(result=invalid), mocked_execution(native_result(**invalid)) as (backend, invoke, _):
                result = backend.sns_execute(ACCOUNT, 'like', params_for('like'), 'stable_unknown')
                self.assertEqual(result['state'], 'unknown')
                self.assertFalse(result['local_accepted'])
                self.assertFalse(result['server_confirmed'])
                self.assertEqual(invoke.call_count, 2)  # probe + 原写请求；无副作用重试。

    def test_publish_requires_nonzero_server_post_id(self):
        for identifier in ('0', None, '', 0, '01', '-1', '18446744073709551616'):
            with self.subTest(server_post_id=identifier), mocked_execution(
                    native_result(server_post_id=identifier)) as (backend, invoke, _):
                result = backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'stable_publish')
                self.assertEqual(result['state'], 'unknown')
                self.assertFalse(result['local_accepted'])
                self.assertFalse(result['server_confirmed'])
                self.assertEqual(invoke.call_count, 2)
        missing = native_result()
        del missing['server_post_id']
        with mocked_execution(missing) as (backend, _, _):
            result = backend.sns_execute(ACCOUNT, 'publish', params_for('publish'), 'missing_post_id')
            self.assertEqual(result['state'], 'unknown')

    def test_disabled_capability_or_account_change_never_invokes_rpc(self):
        with mocked_execution(native_result(), capabilities={'like': False}) as (backend, invoke, read):
            with self.assertRaises(ApiError) as caught:
                backend.sns_execute(ACCOUNT, 'like', params_for('like'), 'disabled_write')
            self.assertEqual((caught.exception.status, caught.exception.code), (503, 'sns_native_unavailable'))
            invoke.assert_not_called()
            read.assert_not_called()
        with mocked_execution(native_result()) as (backend, invoke, read):
            backend.profile.return_value = {'wxid': 'wxid_other_account'}
            with self.assertRaises(ApiError) as caught:
                backend.sns_execute(ACCOUNT, 'like', params_for('like'), 'wrong_account')
            self.assertEqual(caught.exception.code, 'account_changed')
            invoke.assert_not_called()
            read.assert_not_called()

    def test_unready_queue_does_not_submit_business_request(self):
        with mocked_execution(native_result()) as (backend, invoke, _):
            invoke.side_effect = [native_result(error=5)]
            with self.assertRaises(ApiError) as caught:
                backend.sns_execute(ACCOUNT, 'like', params_for('like'), 'unready_probe')
            self.assertEqual(caught.exception.code, 'sns_queue_unavailable')
            self.assertEqual(invoke.call_count, 1)

    def test_validation_rejection_precedes_pins_or_rpc_access(self):
        with mocked_execution(native_result()) as (backend, invoke, read):
            backend.sns_validate.side_effect = ApiError(
                403, 'sns_post_scope_mismatch', '未核实目标作者')
            with self.assertRaises(ApiError) as caught:
                backend.sns_execute(ACCOUNT, 'like', params_for('like'), 'invalid_target')
            self.assertEqual(caught.exception.code, 'sns_post_scope_mismatch')
            invoke.assert_not_called()
            read.assert_not_called()

    def test_validate_rejects_reply_before_any_target_access(self):
        backend = NativeBackend()
        with patch('api.native_sns.validate_target') as target, \
                patch('api.native_sns.validate_friend') as friend:
            with self.assertRaises(ApiError) as caught:
                backend.sns_validate(ACCOUNT, 'comment', {
                    **params_for('comment'), 'reply_comment_id': '123'})
            self.assertEqual((caught.exception.status, caught.exception.code), (400, 'sns_reply_unavailable'))
            target.assert_not_called()
            friend.assert_not_called()

    def test_like_requires_exact_target_and_friend_check(self):
        backend = NativeBackend()
        with patch('api.native_sns.validate_target') as target, \
                patch('api.native_sns.validate_friend') as friend:
            backend.sns_validate(ACCOUNT, 'like', params_for('like'))
            target.assert_called_once_with(ACCOUNT, POST_ID, AUTHOR)
            friend.assert_called_once_with(ACCOUNT, AUTHOR)
        with patch('api.native_sns.validate_target') as target, \
                patch('api.native_sns.validate_friend', side_effect=ApiError(
                    403, 'sns_friend_unverified', '未核实好友')) as friend:
            with self.assertRaises(ApiError) as caught:
                backend.sns_validate(ACCOUNT, 'like', params_for('like'))
            self.assertEqual(caught.exception.code, 'sns_friend_unverified')
            target.assert_called_once()
            friend.assert_called_once()
        with patch('api.native_sns.validate_target') as target, \
                patch('api.native_sns.validate_friend') as friend:
            for action in ('unlike', 'comment', 'publish'):
                backend.sns_validate(ACCOUNT, action, params_for(action))
            self.assertEqual(target.call_count, 2)
            friend.assert_not_called()





if __name__ == '__main__':
    unittest.main()
