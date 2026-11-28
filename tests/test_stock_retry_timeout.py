import json, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_stock as b


class RetryQueuedTimeout(unittest.TestCase):
    def _status(self, at, now):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "results").mkdir()
            (root / "results" / "stock-x-01.json").write_text(json.dumps(
                {"id": "stock-x-01", "status": "retry_queued", "at": at}))
            return b._entry_status({"id": "stock-x-01", "status": "pending"}, root, now)

    def test_old_retry_stops_blocking_refill(self):
        now = datetime(2026, 11, 28, 22, 40, tzinfo=b.IST)
        self.assertEqual(self._status("2026-11-20 19:46 IST", now), "failed")

    def test_fresh_retry_stays_pending(self):
        now = datetime(2026, 11, 28, 22, 40, tzinfo=b.IST)
        self.assertEqual(self._status("2026-11-28 22:00 IST", now), "pending")


if __name__ == "__main__":
    unittest.main()
