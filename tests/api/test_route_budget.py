"""Daily Google route accounting uses temporary local databases only."""
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.api.route_budget import DailyRouteBudget, RouteBudgetExceeded

NOW=datetime(2026,9,14,tzinfo=timezone.utc).timestamp()

class RouteBudgetTests(unittest.TestCase):
    def test_persists_across_instances_and_resets_on_utc_day(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'private'/'budget.sqlite3'
            budget=DailyRouteBudget(path,limit=2,clock=lambda:NOW)
            self.assertFalse(path.parent.exists())
            budget.claim();DailyRouteBudget(path,limit=2,clock=lambda:NOW+3600).claim()
            with self.assertRaises(RouteBudgetExceeded):DailyRouteBudget(path,limit=2,clock=lambda:NOW+86399).claim()
            DailyRouteBudget(path,limit=2,clock=lambda:NOW+86400).claim()
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
    def test_concurrent_instances_cannot_exceed_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'budget.sqlite3'
            def attempt(_):
                try:DailyRouteBudget(path,limit=3,clock=lambda:NOW).claim();return True
                except RouteBudgetExceeded:return False
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(attempt,range(20))),3)
    def test_storage_failure_fails_closed_without_path_in_error(self):
        with tempfile.TemporaryDirectory() as root:
            parent=Path(root)/'PRIVATE_PATH';parent.write_text('not a directory')
            budget=DailyRouteBudget(parent/'budget.sqlite3')
            with self.assertRaises(RouteBudgetExceeded) as found:budget.claim()
            self.assertNotIn('PRIVATE_PATH',str(found.exception))
            self.assertEqual(parent.read_text(),'not a directory')

    def test_live_google_cache_hit_does_not_claim_again_and_limit_blocks_network(self):
        import test_live
        fixture=test_live.LiveTests();fixture.setUp();fixture.read()
        with tempfile.TemporaryDirectory() as root:
            fixture.live._daily_route_budget=DailyRouteBudget(Path(root)/'quota.sqlite3',limit=1,clock=lambda:NOW)
            body=fixture.travel_body('google_routes')
            first=fixture.live.travel(fixture.session,body)
            second=fixture.live.travel(fixture.session,body)
            self.assertTrue(second['travel_pairs'][0]['travel']['cached'])
            self.assertEqual(len(test_live.Routes.calls),1)
            changed={**body,'destination_address':'Different fixture address'}
            fixture.assert_code('routes_budget_exhausted',lambda:fixture.live.travel(fixture.session,changed))
            self.assertEqual(len(test_live.Routes.calls),1)

if __name__=='__main__':unittest.main()
