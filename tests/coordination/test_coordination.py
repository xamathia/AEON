import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/coordination/coordination.py'


class GitIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / 'remote.git'
        self.repo = self.root / 'repo'
        self.env = dict(os.environ)
        self.env.update(GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.com',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.com')
        self.run_cmd(['git', 'init', '--bare', str(self.remote)], self.root)
        self.run_cmd(['git', 'clone', str(self.remote), str(self.repo)], self.root)
        self.git('checkout', '-b', 'main')
        (self.repo / 'shared.txt').write_text('baseline\n')
        self.git('add', '.')
        self.git('commit', '-m', 'baseline')
        self.git('push', '-u', 'origin', 'main')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ['issue', 'view']:
    print(json.dumps({'state':'OPEN','body':'### Propriétaire\\n'+os.getenv('TEST_OWNER','Astra')+'\\n### Fichiers réservés\\n- shared.txt\\n'}))
elif args[:2] == ['pr', 'list']:
    print(os.getenv('TEST_PRS','[]'))
elif args[:2] == ['pr', 'view']:
    if os.getenv('TEST_MERGE_LOG') and os.path.exists(os.environ['TEST_MERGE_LOG']):
        print(json.dumps({'state':'MERGED','mergeCommit':{'oid':'1'*40},'url':'https://example.com/pr/4'}))
    else:
        print(os.getenv('TEST_PR','{}'))
elif args[:2] == ['pr', 'merge']:
    with open(os.environ['TEST_MERGE_LOG'], 'w') as f:
        json.dump(args, f)
elif args[:2] == ['repo', 'view']:
    print(json.dumps({'nameWithOwner':'example/aeon'}))
elif args[:2] == ['variable', 'get']:
    print('astra-login')
elif args[:2] == ['api', 'user']:
    print(os.getenv('TEST_LOGIN','astra-login'))
elif args[0] == 'api' and any(a.endswith('/files') for a in args):
    print('shared.txt')
elif args[0] == 'api' and 'comments' in args[1]:
    print(os.getenv('TEST_REVIEWS','[[]]'))
else:
    sys.exit(2)
''')
        gh.chmod(0o755)
        self.env['PATH'] = str(self.bin) + os.pathsep + self.env['PATH']

    def run_cmd(self, args, cwd=None, ok=True):
        p = subprocess.run(args, cwd=cwd or self.repo, env=self.env,
                           text=True, capture_output=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def git(self, *args, cwd=None):
        return self.run_cmd(['git', *args], cwd).stdout.strip()

    def cli(self, *args, cwd=None, ok=True):
        return self.run_cmd(['python3', str(SCRIPT), *args], cwd, ok)

    def start(self):
        self.work = self.root / 'task'
        self.cli('start-task', 'astra', '2', 'calendar', '--path', str(self.work))
        return self.work

    def test_start_creates_isolated_branch_and_metadata(self):
        work = self.start()
        self.assertEqual(self.git('branch', '--show-current', cwd=work), 'agent/astra/2-calendar')
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual(self.git('status', '--porcelain', cwd=work), '')
        result = self.cli('sync-task', cwd=work)
        self.assertIn('Synchronized', result.stdout)

    def test_dirty_worktree_prevents_creation(self):
        (self.repo / 'untracked').write_text('keep me')
        p = self.cli('start-task', 'astra', '2', 'calendar', ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('clean', p.stderr)
        self.assertEqual((self.repo / 'untracked').read_text(), 'keep me')

    def test_wrong_owner_prevents_creation(self):
        self.env['TEST_OWNER'] = 'Sol'
        p = self.cli('start-task', 'astra', '2', 'calendar', ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('owner', p.stderr)

    def test_reserved_path_collision_prevents_creation(self):
        self.env['TEST_PRS'] = json.dumps([{'number': 9, 'headRefName': 'agent/sol/3-other',
            'body': '## Fichiers réservés\n- shared.txt\n', 'files': []}])
        p = self.cli('start-task', 'astra', '2', 'calendar', ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('collision', p.stderr.lower())

    def test_rebase_conflict_is_preserved_for_resolution(self):
        work = self.start()
        (work / 'shared.txt').write_text('task\n')
        self.git('add', '.', cwd=work)
        self.git('commit', '-m', 'task', cwd=work)
        (self.repo / 'shared.txt').write_text('main change\n')
        (self.repo / 'unrelated.txt').write_text('main-owned\n')
        self.git('add', '.')
        self.git('commit', '-m', 'main change')
        self.git('push')
        p = self.cli('sync-task', cwd=work, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('shared.txt', p.stdout + p.stderr)
        self.assertIn('shared.txt', self.git('diff', '--name-only', '--diff-filter=U', cwd=work))
        (work / 'shared.txt').write_text('resolved\n')
        self.git('add', 'shared.txt', cwd=work)
        self.env['GIT_EDITOR'] = 'true'
        self.git('rebase', '--continue', cwd=work)
        self.assertIn('Synchronized', self.cli('sync-task', cwd=work).stdout)
        self.assertEqual((work / 'unrelated.txt').read_text(), 'main-owned\n')

    def test_handoff_records_failed_test_and_finish_refuses(self):
        work = self.start()
        p = self.cli('handoff', '--test', 'python3 -c "raise SystemExit(7)"', cwd=work, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('FAILED', p.stdout)
        p = self.cli('finish-task', cwd=work, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('validation', p.stderr.lower())

    def test_finish_accepts_only_published_green_nondraft_pr(self):
        work = self.start()
        self.cli('handoff', '--test', 'python3 -c "pass"', cwd=work)
        pr = {'number': 4, 'url': 'https://example.com/pr/4', 'state': 'OPEN',
              'isDraft': False, 'headRefName': 'agent/astra/2-calendar',
              'headRefOid': self.git('rev-parse', 'HEAD', cwd=work), 'baseRefName': 'main',
              'body': '## Issue\nCloses #2\n## Agent\nAstra\n## Fichiers réservés\n- shared.txt\n',
              'statusCheckRollup': [{'name': 'Coordination', 'conclusion': 'SUCCESS'}, {'name': 'Tests', 'conclusion': 'SUCCESS'}], 'mergeable': 'MERGEABLE'}
        self.env['TEST_PR'] = json.dumps(pr)
        sha = pr['headRefOid']
        review = {'user': {'login': 'astra-login'}, 'body': f'AEON-REVIEW: Astra\nCommit: {sha}\nVerdict: APPROVED\nAnalysis: reviewed'}
        self.env['TEST_REVIEWS'] = json.dumps([[review]])
        self.assertIn('Astra review', self.cli('finish-task', cwd=work).stdout)
        for reviews in ([], [dict(review, user={'login': 'someone-else'})],
                        [dict(review, body=review['body'].replace(sha, '0' * 40))],
                        [review, dict(review, body=review['body'].replace('APPROVED', 'CHANGES_REQUESTED'))]):
            with self.subTest(reviews=reviews):
                self.env['TEST_REVIEWS'] = json.dumps([reviews])
                self.assertNotEqual(self.cli('finish-task', cwd=work, ok=False).returncode, 0)
        self.env['TEST_REVIEWS'] = json.dumps([[review]])
        # Astra finishes from the main clone without metadata from the Sol worktree.
        self.git('push', 'origin', 'HEAD:refs/pull/4/head', cwd=work)
        self.assertIn('Astra review', self.cli('finish-task', '--pr', '4', cwd=self.repo).stdout)
        self.env['TEST_LOGIN'] = 'sol-login'
        self.assertNotEqual(self.cli('finish-task', '--pr', '4', '--merge', cwd=self.repo, ok=False).returncode, 0)
        self.env['TEST_LOGIN'] = 'astra-login'
        self.env['TEST_MERGE_LOG'] = str(self.root / 'merge.json')
        self.assertIn('DONE', self.cli('finish-task', '--pr', '4', '--merge', cwd=self.repo).stdout)
        merge_args = json.loads((self.root / 'merge.json').read_text())
        self.assertEqual(merge_args[-2:], ['--match-head-commit', sha])
        (self.root / 'merge.json').unlink()
        for change in ({'isDraft': True}, {'headRefOid': '0' * 40},
                       {'statusCheckRollup': []}, {'statusCheckRollup': [{'conclusion': None}]},
                       {'mergeable': 'CONFLICTING'}):
            with self.subTest(change=change):
                self.env['TEST_PR'] = json.dumps(dict(pr, **change))
                self.assertNotEqual(self.cli('finish-task', cwd=work, ok=False).returncode, 0)

    def test_finish_rejects_stale_validation(self):
        work = self.start()
        self.cli('handoff', '--test', 'python3 -c "pass"', cwd=work)
        (work / 'shared.txt').write_text('new\n')
        self.git('add', '.', cwd=work)
        self.git('commit', '-m', 'new', cwd=work)
        p = self.cli('finish-task', cwd=work, ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('validation', p.stderr.lower())


if __name__ == '__main__':
    unittest.main()
