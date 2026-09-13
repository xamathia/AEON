"""Check GitHub inputs and reservations at the network boundary."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/coordination'))
import coordination
import validate


class ReservationTests(unittest.TestCase):
    def test_issue_template_labels_are_consumed_by_coordination(self):
        template = json.loads((ROOT / '.github/ISSUE_TEMPLATE/agent-task.yml').read_text())
        fields = {item['id']: item['attributes']['label'] for item in template['body']}
        body = '### ' + fields['owner'] + '\nAstra\n### ' + fields['paths'] + '\n- src/\n'
        with patch.object(coordination, 'gh', return_value={'state': 'OPEN', 'body': body}):
            self.assertEqual(coordination.issue_info(2, 'astra'), ['src/'])

    def test_reservations_accept_english_and_legacy_french_headings(self):
        for heading in ('Reserved files', 'Fichiers réservés'):
            with self.subTest(heading=heading):
                self.assertEqual(coordination.paths('## ' + heading + '\n- src/\n- README.md\n'),
                                 ['src/', 'README.md'])

    def test_issue_owner_accepts_english_and_legacy_french_headings(self):
        for owner, reserved in (('Owner', 'Reserved files'), ('Propriétaire', 'Fichiers réservés')):
            body = '### ' + owner + '\nAstra\n### ' + reserved + '\n- src/\n'
            with self.subTest(owner=owner), patch.object(coordination, 'gh', return_value={'state': 'OPEN', 'body': body}):
                self.assertEqual(coordination.issue_info(2, 'astra'), ['src/'])

    def test_directory_boundaries_do_not_capture_similar_prefix(self):
        self.assertTrue(coordination.overlaps('src/calendar/', 'src/calendar/api.py'))
        self.assertFalse(coordination.overlaps('src/calendar/', 'src/calendar-old/api.py'))
        self.assertFalse(coordination.overlaps('src/calendar', 'src/calendar/api.py'))

    def test_unsafe_or_ambiguous_reservations_rejected(self):
        for value in ('../secret', '/tmp', '.git/config', 'src/*', './src/', 'src//x', 'src/../x'):
            with self.subTest(value=value), self.assertRaises(coordination.CoordinationError):
                coordination.paths('## Fichiers réservés\n- ' + value)

    def test_conflict_marker_in_tracked_document_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', str(root)], capture_output=True, check=True)
            for filename in validate.REQUIRED:
                target = root / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / filename).read_bytes())
            (root / 'README.md').write_text('before\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> topic\n')
            with self.assertRaisesRegex(coordination.CoordinationError, 'Conflict marker'):
                validate.validate_files(root)


class PRValidation(unittest.TestCase):
    def setUp(self):
        self.event = {'repository': {'full_name': 'example/aeon'}, 'pull_request': {'number': 4}}
        self.pr = {'body': '## Issue\nCloses #2\n## Agent\nSol\n## Fichiers réservés\n- src/\n',
                   'headRefName': 'agent/sol/2-feature', 'baseRefName': 'main', 'files': []}
        self.issue = {'state': 'OPEN', 'body': '### Propriétaire\nSol\n### Fichiers réservés\n- src/\n'}
        self.other = []
        self.files = 'src/new.py\n'

    def github(self, *args):
        if args[:2] == ('pr', 'view'):
            return self.pr
        if args[:2] == ('issue', 'view'):
            return self.issue
        if args[:2] == ('pr', 'list'):
            return self.other
        self.fail(str(args))

    def check(self):
        with patch.object(validate, 'gh', side_effect=self.github), patch.object(validate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, self.files, '')):
            validate.validate_pr(self.event)

    def test_matching_issue_scope_and_branch_pass(self):
        self.check()

    def test_english_and_french_issue_pr_combinations_remain_compatible(self):
        original_pr, original_issue = self.pr['body'], self.issue['body']
        for english_pr, english_issue in ((True, True), (True, False), (False, True), (False, False)):
            self.pr['body'] = original_pr.replace('Fichiers réservés', 'Reserved files') if english_pr else original_pr
            self.issue['body'] = (original_issue.replace('Propriétaire', 'Owner').replace('Fichiers réservés', 'Reserved files')
                                  if english_issue else original_issue)
            with self.subTest(english_pr=english_pr, english_issue=english_issue):
                self.check()

    def test_english_reservations_still_detect_legacy_pr_collisions(self):
        self.pr['body'] = self.pr['body'].replace('Fichiers réservés', 'Reserved files')
        self.other = [{'number': 7, 'body': '## Fichiers réservés\n- src/api/\n', 'files': []}]
        with self.assertRaisesRegex(coordination.CoordinationError, 'Collision'):
            self.check()

    def test_missing_issue_rejected(self):
        self.pr['body'] = self.pr['body'].replace('Closes #2', 'See issue later')
        with self.assertRaisesRegex(coordination.CoordinationError, 'Closes'):
            self.check()

    def test_wrong_owner_rejected(self):
        self.issue['body'] = self.issue['body'].replace('Sol', 'Astra')
        with self.assertRaisesRegex(coordination.CoordinationError, 'owner'):
            self.check()

    def test_rename_source_outside_scope_rejected(self):
        self.files = 'src/new.py\nprivate/old.py\n'
        with self.assertRaisesRegex(coordination.CoordinationError, 'outside reservations'):
            self.check()

    def test_collision_with_other_pr_rejected(self):
        self.other = [{'number': 7, 'body': '## Fichiers réservés\n- src/api/\n', 'files': []}]
        with self.assertRaisesRegex(coordination.CoordinationError, 'Collision'):
            self.check()

    def test_different_issue_and_pr_reservations_rejected(self):
        self.issue['body'] = self.issue['body'].replace('src/', 'docs/')
        with self.assertRaisesRegex(coordination.CoordinationError, 'differ'):
            self.check()


if __name__ == '__main__':
    unittest.main()
