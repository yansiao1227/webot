"""发布引擎回归：仅临时SQLite、冻结源码和模拟SSH/公网，不访问服务器。"""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import shlex
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from daily_digest.content import validate_edition
from daily_digest.deploy.engine import DeploymentError, SnapshotDeployer, WEB_FILES, DEPLOY_FILES, ROOT
from daily_digest.examples.build_preview import sample


def edition(day='2026-11-06', image=None):
    value = json.loads(json.dumps(sample()).replace('2030-01-02', day))
    value['simulation'] = False
    value.pop('content_hash', None)
    if image:
        value['sources'][0]['image'] = {'url': 'https://example.com/picture.webp', 'caption': '来源配图',
                                      'credit': '公开来源', 'asset_id': hashlib.sha256(image).hexdigest()}
    return validate_edition(value)


class Remote:
    def __init__(self):
        self.phase = 'absent'
        self.release_id = ''
        self.calls = []
        self.editions = {}
        self.media = {}
        self.fail_activate_response = False
        self.fail_commit_response = False
        self.fail_upload = False
        self.bad_public_media = False

    def run(self, command, **options):
        self.calls.append(command)
        assert options['timeout'] <= 300
        assert 'BatchMode=yes' in command and 'IdentitiesOnly=yes' in command and 'StrictHostKeyChecking=yes' in command
        if command[0] == 'scp':
            if self.fail_upload:
                raise subprocess.TimeoutExpired(command, 120)
            return SimpleNamespace(stdout='')
        operation = command[-1]
        if 'release.sh activate ' in operation:
            self.phase = 'activated'
            if self.fail_activate_response:
                self.fail_activate_response = False
                raise subprocess.TimeoutExpired(command, 300)
        elif 'release.sh commit ' in operation:
            self.phase = 'confirmed'
            if self.fail_commit_response:
                self.fail_commit_response = False
                raise subprocess.TimeoutExpired(command, 90)
        elif 'release.sh rollback ' in operation:
            self.phase = 'rolled_back'
        return SimpleNamespace(stdout=json.dumps({'phase': self.phase, 'current_release': self.release_id}) + '\n')

    def read(self, url):
        path = urlsplit(url).path
        if path == '/api/editions':
            return json.dumps({'items': [{'date': k, 'content_hash': v['content_hash']} for k, v in self.editions.items()],
                               'next_cursor': None}).encode()
        if path == '/api/editions/latest':
            return json.dumps(self.editions[max(self.editions)]).encode()
        if path.startswith('/api/editions/'):
            return json.dumps(self.editions[path.rsplit('/', 1)[1]]).encode()
        if path.startswith('/media/'):
            return b'wrong image' if self.bad_public_media else self.media[path.rsplit('/', 1)[1]]
        return b'<!doctype html><html>Daily Observer</html>'


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.web = self.root / 'daily_digest/web'
        for relative in WEB_FILES:
            target = self.web / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = ROOT / 'daily_digest/web' / relative
            if relative == 'dist/server/index.js':
                target.write_text('export default {fetch(){return new Response("ok")}};', encoding='utf-8')
            else:
                shutil.copy2(source, target)
        for relative in DEPLOY_FILES:
            target = self.root / 'daily_digest/deploy' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / 'daily_digest/deploy' / relative, target)
        (self.web / 'package.json').write_text('{"name":"fake-web-build","private":true}', encoding='utf-8')
        (self.web / 'src').mkdir()
        (self.web / 'src/main.tsx').write_text('version-one', encoding='utf-8')
        self.build_calls = []
        self.database = self.root / 'preview.sqlite'
        self.db = sqlite3.connect(self.database)
        self.db.executescript((self.web / 'migrations/0001_editions.sql').read_text())
        self.db.executescript((self.web / 'migrations/0002_media.sql').read_text())
        self.remote = Remote()
        self.engine = self.new_engine()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def new_engine(self):
        return SnapshotDeployer('192.0.2.50', database=self.database,
                                state_dir=self.root / 'releases', root=self.root,
                                run_command=self.remote.run, read_url=self.remote.read,
                                build_command=self.build)

    def build(self, command, **options):
        self.build_calls.append((command, options))
        if command[-1] == 'build':
            value = (self.web / 'src/main.tsx').read_text(encoding='utf-8')
            (self.web / 'dist/server/index.js').write_text('export default ' + json.dumps(value) + ';', encoding='utf-8')
        return SimpleNamespace(stdout='')

    def put(self, value):
        self.db.execute('INSERT INTO editions(date,content_hash,title,cutoff_at,body) VALUES(?,?,?,?,?)',
                        (value['date'], value['content_hash'], value['title'], value['cutoff_at'], json.dumps(value)))
        self.db.commit()

    def ready(self, image=None):
        value = edition(image=image)
        self.put(value)
        if image:
            digest = hashlib.sha256(image).hexdigest()
            self.db.execute('INSERT INTO media VALUES(?,?,?)', (digest, image, 'image/webp'))
            self.db.commit()
            self.remote.media[digest] = image
        release = self.engine.prepare()
        self.remote.release_id = release.release_id
        self.remote.editions[value['date']] = value
        return value, release

    def test_prepare_freezes_database_and_reuses_stable_id_across_restart(self):
        first, release = self.ready()
        self.assertEqual(self.new_engine().prepare().release_id, release.release_id)
        self.assertEqual(self.new_engine().load_release(release.release_id).manifest, release.manifest)
        self.put(edition('2026-11-07'))
        self.assertEqual(release.manifest['latest_date'], first['date'])
        with closing(sqlite3.connect(release.directory / 'editions.sqlite')) as frozen:
            self.assertEqual(frozen.execute('SELECT COUNT(*) FROM editions').fetchone()[0], 1)
        self.assertNotEqual(self.engine.prepare().release_id, release.release_id)
        self.assertFalse(self.remote.calls)

    def test_changed_web_source_creates_a_different_stable_release(self):
        _, first = self.ready()
        (self.web / 'src/main.tsx').write_text('version-two', encoding='utf-8')
        self.assertNotEqual(self.engine.prepare().release_id, first.release_id)

    def test_verified_template_reuses_build_and_dist_change_forces_validation(self):
        _, release = self.ready()
        self.assertEqual([args[0][-1] for args in self.build_calls], ['check', 'test', 'build'])
        self.new_engine().prepare()
        self.assertEqual(len(self.build_calls), 3)
        (self.web / 'dist/server/index.js').write_text('tampered build', encoding='utf-8')
        self.assertEqual(self.new_engine().prepare().release_id, release.release_id)
        self.assertEqual([args[0][-1] for args in self.build_calls], ['check', 'test', 'build'] * 2)

    def test_cli_and_automatic_release_directories_share_build_evidence(self):
        self.ready()
        automatic = self.new_engine()
        automatic.state_dir = self.root / 'daily_digest/state/live/ssh-publisher/releases'
        automatic.prepare()
        self.assertEqual(len(self.build_calls), 3)

    def test_runtime_secret_and_site_metadata_changes_do_not_rebuild_or_enter_build_environment(self):
        with patch.dict('os.environ', {'MCP_BRIDGE_TOKEN': 'test-secret', 'OPENAI_API_KEY': 'test-secret',
                                       'DAILY_DIGEST_DEPLOY_IDENTITY_FILE': 'private-test-path'}):
            _, first = self.ready()
        for _, options in self.build_calls:
            self.assertLessEqual(options['timeout'], 180)
            self.assertFalse({'MCP_BRIDGE_TOKEN', 'OPENAI_API_KEY', 'DAILY_DIGEST_DEPLOY_IDENTITY_FILE'} & options['env'].keys())
        for name in ('state', '.openai', 'node_modules'):
            directory = self.web / name
            directory.mkdir(exist_ok=True)
            (directory / 'ignore.json').write_text('runtime-test-data', encoding='utf-8')
        (self.web / '.env').write_text('TEST_ONLY_SECRET=not-forwarded', encoding='utf-8')
        self.assertEqual(self.new_engine().prepare().release_id, first.release_id)
        self.assertEqual(len(self.build_calls), 3)

    def test_failed_template_validation_preserves_old_evidence_and_does_not_prepare_release(self):
        _, release = self.ready()
        proof = self.root / 'daily_digest/state/web-build.json'
        evidence = proof.read_bytes()
        (self.web / 'src/main.tsx').write_text('broken-new-version', encoding='utf-8')
        failed = self.new_engine()
        def stop(command, **options):
            raise subprocess.CalledProcessError(1, command)
        failed.build_command = stop
        with self.assertRaisesRegex(DeploymentError, 'web_check_or_build_failed'):
            failed.prepare()
        self.assertEqual(proof.read_bytes(), evidence)
        self.assertEqual(len(list(self.engine.state_dir.glob('d*/manifest.json'))), 1)
        self.assertFalse(self.remote.calls)

    def test_prepare_rejects_conflicting_body_media_and_capacity(self):
        value = edition()
        self.put(value)
        self.db.execute("UPDATE editions SET content_hash=?", ('0' * 64,)); self.db.commit()
        with self.assertRaises(DeploymentError):
            self.engine.prepare()
        self.db.execute('DELETE FROM editions'); self.db.commit()
        image = (ROOT / 'daily_digest/web/src/assets/news-cosmos.webp').read_bytes()
        self.put(edition(image=image))
        with self.assertRaisesRegex(DeploymentError, 'media_missing'):
            self.engine.prepare()
        with patch('daily_digest.deploy.engine.MAX_SNAPSHOT_BYTES', 1):
            with self.assertRaisesRegex(DeploymentError, 'capacity'):
                self.engine.prepare()

    def test_frozen_files_or_bundle_cannot_be_silently_replaced(self):
        _, release = self.ready()
        release.bundle.write_bytes(b'corrupted archive')
        with self.assertRaisesRegex(DeploymentError, 'bundle_changed'):
            self.engine.deploy(release)
        self.assertFalse(self.remote.calls)

    def test_ssh_two_phase_confirmed_only_after_public_history_and_media(self):
        image = (ROOT / 'daily_digest/web/src/assets/news-cosmos.webp').read_bytes()
        value, release = self.ready(image)
        result = self.engine.deploy(release)
        self.assertEqual(result, {'state': 'confirmed', 'release_id': release.release_id,
                                 'date': value['date'], 'content_hash': value['content_hash'],
                                 'site_url': 'http://192.0.2.50', 'url': 'http://192.0.2.50/daily/' + value['date']})
        self.assertEqual(self.remote.phase, 'confirmed')
        self.assertEqual(self.engine.status()['releases'][0]['state'], 'confirmed')
        self.assertEqual(sum(call[0] == 'scp' for call in self.remote.calls), 1)
        self.assertIn('ConnectTimeout=10', self.remote.calls[0])

    def test_response_lost_after_activate_queries_original_release_without_second_upload(self):
        _, release = self.ready()
        self.remote.fail_activate_response = True
        self.assertEqual(self.engine.deploy(release)['state'], 'confirmed')
        self.assertEqual(sum('release.sh activate ' in call[-1] for call in self.remote.calls), 1)
        self.assertEqual(sum(call[0] == 'scp' for call in self.remote.calls), 1)
        activation = next(call[-1] for call in self.remote.calls if 'release.sh activate ' in call[-1])
        self.assertIn('nohup sh ', activation)
        self.assertIn('</dev/null', activation)
        self.assertIn('wait "$activate_pid"', activation)

    def test_response_lost_after_commit_finishes_by_verify_only_after_restart(self):
        _, release = self.ready()
        self.remote.fail_commit_response = True
        with self.assertRaises(DeploymentError):
            self.engine.deploy(release)
        count = len(self.remote.calls)
        self.assertEqual(self.new_engine().verify(release)['state'], 'confirmed')
        self.assertTrue(all(call[0] == 'ssh' and 'release.sh status ' in call[-1] for call in self.remote.calls[count:]))

    def test_unknown_upload_without_remote_activation_never_claims_success(self):
        _, release = self.ready()
        self.remote.fail_upload = True
        with self.assertRaises(DeploymentError):
            self.engine.deploy(release)
        count = len(self.remote.calls)
        with self.assertRaises(DeploymentError):
            self.new_engine().verify(release)
        self.assertTrue(all('release.sh status ' in call[-1] for call in self.remote.calls[count:]))
        self.assertFalse(any('release.sh activate ' in call[-1] for call in self.remote.calls))

    def test_public_mismatch_rolls_back_before_confirmation(self):
        image = (ROOT / 'daily_digest/web/src/assets/news-cosmos.webp').read_bytes()
        _, release = self.ready(image)
        self.remote.bad_public_media = True
        with self.assertRaisesRegex(DeploymentError, 'public_verification_failed'):
            self.engine.deploy(release)
        self.assertEqual(self.remote.phase, 'rolled_back')
        self.assertFalse(any('release.sh commit ' in call[-1] for call in self.remote.calls))
        self.assertEqual(self.engine.status()['releases'][0]['state'], 'rolled_back')

    def test_remote_history_conflict_stops_before_scp_or_switch(self):
        _, release = self.ready()
        conflicting = edition('2026-11-07')
        self.remote.editions[conflicting['date']] = conflicting
        with self.assertRaisesRegex(DeploymentError, 'public_history_missing_or_conflicting'):
            self.engine.deploy(release)
        self.assertFalse(any(call[0] == 'scp' or 'release.sh activate ' in call[-1] for call in self.remote.calls))

    def test_target_changes_and_invalid_identifiers_are_rejected(self):
        _, release = self.ready()
        engine = self.new_engine(); engine.host = 'other.example'
        with self.assertRaisesRegex(DeploymentError, 'target_changed'):
            engine.load_release(release.release_id)
        with self.assertRaises(ValueError):
            self.engine.load_release('../other')
        with self.assertRaises(ValueError):
            SnapshotDeployer('host;touch x')

    def test_remote_locked_history_check_accepts_only_a_matching_subset(self):
        script = """
import assert from 'node:assert/strict';
import { verifyHistory } from './daily_digest/deploy/verify-release.mjs';
const manifest={editions:{'2026-11-06':'aaa','2026-11-07':'bbb'}};
await verifyHistory(manifest,async()=>({items:[{date:'2026-11-06',content_hash:'aaa'}],next_cursor:null}));
await assert.rejects(()=>verifyHistory(manifest,async()=>({items:[{date:'2026-11-08',content_hash:'new'}],next_cursor:null})),/current_history_missing_or_conflicting/);
await assert.rejects(()=>verifyHistory(manifest,async()=>({items:[{date:'2026-11-06',content_hash:'wrong'}],next_cursor:null})),/current_history_missing_or_conflicting/);
await assert.rejects(()=>verifyHistory(manifest,async()=>({items:[],next_cursor:'2026-11-06'})),/history_cursor_invalid/);
console.log('history-check-ok');
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT,
                                check=True, capture_output=True, text=True, timeout=10)
        self.assertIn('history-check-ok', result.stdout)

    def test_interrupted_activation_finalizes_without_new_activate(self):
        _, release = self.ready()
        self.remote.phase = 'activating'
        self.assertEqual(self.engine.verify(release)['state'], 'confirmed')
        self.assertFalse(any(call[0] == 'scp' or 'release.sh activate ' in call[-1] for call in self.remote.calls))

    @unittest.skipUnless(shutil.which('bash'), '需要本地bash验证SSH父进程断连语义')
    def test_detached_activation_finishes_once_after_foreground_hangup(self):
        _, release = self.ready()
        fake = self.root / 'detached-controller'
        fake.mkdir()
        script = '''#!/bin/sh
cd "$(dirname "$0")"
if [ "$1" = activate ]; then
 printf started > old-stopped
 sleep 0.3
 printf activated > new-running
 printf '1\\n' >> activation-count
fi
printf '{"phase":"activated"}\\n'
'''
        (fake / 'release.sh').write_text(script, encoding='utf-8', newline='\n')
        prefix = '/opt/daily-digest/releases/' + release.release_id
        operation = self.engine._activation_command(release).replace(prefix, shlex.quote(fake.as_posix()))
        marker = shlex.quote((fake / 'old-stopped').as_posix())
        # End the SSH-equivalent foreground shell exactly after the old-service phase.
        command = (operation + ' & foreground=$!; n=0; '
                   f'while [ ! -f {marker} ]; do n=$((n+1)); [ "$n" -lt 100 ] || exit 1; sleep 0.02; done; '
                   'kill -HUP "$foreground"; wait "$foreground" 2>/dev/null || true')
        subprocess.run(['bash', '-c', command], check=True, capture_output=True, text=True, timeout=10)
        until = time.monotonic() + 5
        while not (fake / 'new-running').exists() and time.monotonic() < until:
            time.sleep(.02)
        self.assertEqual((fake / 'new-running').read_text(), 'activated')
        self.assertEqual((fake / 'activation-count').read_text().splitlines(), ['1'])


if __name__ == '__main__':
    unittest.main()
