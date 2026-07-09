"""Unit tests for the run-history persistence module."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run_history  # noqa: E402


class TestRecordAndLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "run_history.jsonl")
        # Point the module's history file at our temp file.
        self._orig = run_history.HISTORY_PATH
        run_history.HISTORY_PATH = self.path

    def tearDown(self):
        run_history.HISTORY_PATH = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_record_writes_jsonl(self):
        rec = run_history.record("won", difficulty="beginner", width=9,
                                 height=9, num_mines=10, model="m",
                                 provider="openai", moves=12, revealed=71,
                                 duration_s=3.5, seed=1)
        self.assertEqual(rec["result"], "won")
        self.assertEqual(rec["size"], "9x9")
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 1)

    def test_load_all_reads_records(self):
        for i in range(3):
            run_history.record("won" if i else "lost", difficulty="expert",
                             width=30, height=16, num_mines=99,
                             model="m", provider="openai", moves=i,
                             revealed=100, duration_s=1.0)
        recs = run_history.load_all()
        self.assertEqual(len(recs), 3)

    def test_summarize_empty(self):
        self.assertEqual(run_history.summarize([]), "尚无对局记录。")

    def test_summarize_counts(self):
        recs = [
            {"result": "won", "moves": 10, "duration_s": 2.0},
            {"result": "won", "moves": 20, "duration_s": 4.0},
            {"result": "lost", "moves": 5, "duration_s": 1.0},
        ]
        s = run_history.summarize(recs)
        self.assertIn("对局 3 局", s)
        self.assertIn("胜 2", s)
        self.assertIn("负 1", s)
        # average moves = 35/3 = 11.7
        self.assertIn("11.7", s)


if __name__ == "__main__":
    unittest.main()
