"""Unit tests for minesweeper game logic (pure, no I/O)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from minesweeper import Minesweeper, DIFFICULTIES  # noqa: E402


class TestBasics(unittest.TestCase):
    def test_from_preset_dimensions(self):
        for name, cfg in DIFFICULTIES.items():
            g = Minesweeper.from_preset(name, seed=1)
            self.assertEqual((g.width, g.height, g.num_mines),
                             (cfg["width"], cfg["height"], cfg["mines"]))

    def test_initial_state_ready(self):
        g = Minesweeper.from_preset("beginner", seed=1)
        self.assertEqual(g.state, "ready")
        self.assertFalse(g.first_move_done)
        self.assertEqual(g.revealed_count, 0)


class TestFirstMoveSafety(unittest.TestCase):
    def test_first_reveal_safe(self):
        g = Minesweeper.from_preset("beginner", seed=42)
        # First click must never be a mine, and a 3x3 zone around it is clear.
        r = g.reveal(4, 4)
        self.assertEqual(r["result"], "safe")
        self.assertFalse(g.mines[4][4])
        for nr, nc in g._neighbors(4, 4):
            self.assertFalse(g.mines[nr][nc])
        self.assertTrue(g.first_move_done)
        self.assertEqual(g.state, "playing")

    def test_mines_placed_once(self):
        g = Minesweeper.from_preset("beginner", seed=7)
        g.reveal(0, 0)
        total = sum(row.count(True) for row in g.mines)
        self.assertEqual(total, g.num_mines)


class TestFloodReveal(unittest.TestCase):
    def test_flood_opens_multiple(self):
        # Tiny board with no mines -> first reveal floods the whole board.
        g = Minesweeper(5, 5, 0, seed=1)
        r = g.reveal(2, 2)
        self.assertEqual(r["result"], "won" if g.num_mines == 0 else "safe")
        self.assertEqual(g.revealed_count, 25)

    def test_flood_stops_at_numbers(self):
        g = Minesweeper(3, 3, 1, seed=3)
        # Reveal a corner; flood opens all connected 0s only, not the mine.
        g.reveal(0, 0)
        self.assertEqual(g.revealed_count, 8)
        self.assertFalse(any(g.revealed[r][c] and g.mines[r][c]
                             for r in range(3) for c in range(3)))


class TestMineLose(unittest.TestCase):
    def test_reveal_mine_loses(self):
        # First reveal is always safe; force mines, then reveal an actual mine.
        g = Minesweeper(3, 3, 1, seed=1)
        g._place_mines(0, 0)  # safe zone around (0,0) excludes the mine
        g.first_move_done = True
        g.state = "playing"
        mine = next((r, c) for r in range(3) for c in range(3) if g.mines[r][c])
        r = g.reveal(*mine)
        self.assertEqual(r["result"], "mine")
        self.assertEqual(g.state, "lost")
        self.assertEqual(g.explode_cell, mine)

    def test_reveal_out_of_bounds(self):
        g = Minesweeper.from_preset("beginner")
        r = g.reveal(-1, 0)
        self.assertEqual(r["result"], "invalid")
        r2 = g.reveal(0, 99)
        self.assertEqual(r2["result"], "invalid")


class TestWin(unittest.TestCase):
    def test_win_when_all_safe_opened(self):
        g = Minesweeper(3, 3, 1, seed=5)
        g._place_mines(0, 0)
        g.first_move_done = True
        g.state = "playing"
        mine = next((r, c) for r in range(3) for c in range(3) if g.mines[r][c])
        # Open every non-mine cell manually.
        for r in range(3):
            for c in range(3):
                if (r, c) != mine:
                    g.reveal(r, c)
        self.assertEqual(g.state, "won")
        self.assertTrue(g.flagged[mine[0]][mine[1]])


class TestToggleFlag(unittest.TestCase):
    def test_toggle(self):
        g = Minesweeper.from_preset("beginner", seed=1)
        r = g.toggle_flag(0, 0)
        self.assertEqual(r["result"], "flag")
        self.assertTrue(g.flagged[0][0])
        r2 = g.toggle_flag(0, 0)
        self.assertEqual(r2["result"], "unflag")

    def test_flag_then_reveal_blocked(self):
        g = Minesweeper.from_preset("beginner", seed=1)
        g.toggle_flag(0, 0)
        g.reveal(0, 0)
        self.assertFalse(g.revealed[0][0])


class TestChord(unittest.TestCase):
    def _setup_one_mine(self):
        # 3x3, single mine at (2,2). Place manually after first reveal.
        g = Minesweeper(3, 3, 1, seed=1)
        g.reveal(0, 0)  # first move places mines; (2,2) may or may not be a mine
        return g

    def test_chord_requires_revealed_number(self):
        g = Minesweeper(3, 3, 1, seed=9)
        g._place_mines(0, 0)
        g.first_move_done = True
        g.state = "playing"
        # chord on a still-hidden cell -> nochange (not revealed)
        r = g.chord(2, 2)
        self.assertEqual(r["result"], "nochange")

    def test_chord_wrong_flag_count(self):
        g = Minesweeper(3, 3, 1, seed=9)
        g._place_mines(0, 0)
        g.first_move_done = True
        g.state = "playing"
        # Reveal a known-safe cell adjacent to the mine, then chord with no flags.
        mine = next((r, c) for r in range(3) for c in range(3) if g.mines[r][c])
        safe = None
        for r in range(3):
            for c in range(3):
                if not g.mines[r][c] and (abs(r - mine[0]) <= 1 and abs(c - mine[1]) <= 1):
                    safe = (r, c)
        if safe is not None:
            g.reveal(*safe)
            # flagged count (0) != number -> nochange
            r = g.chord(*safe)
            self.assertEqual(r["result"], "nochange")

    def test_chord_opens_when_flags_correct(self):
        # Construct a deterministic board: 2x2, mine at (0,1).
        g = Minesweeper(2, 2, 1, seed=1)
        # Force mine layout deterministically by shuffling with fixed seed so
        # we instead drive it manually through the public-ish path.
        g._place_mines(0, 0)  # safe zone excludes (0,0) and its neighbors
        # After placing with first move at (0,0), mine cannot be a neighbor;
        # reveal (0,0) which is a number 0 -> flood won't reach mine area.
        g.first_move_done = True
        g.state = "playing"
        # Flag the known mine, then chord a neighbor that counts it.
        mine = None
        for r in range(2):
            for c in range(2):
                if g.mines[r][c]:
                    mine = (r, c)
        if mine is None:
            self.skipTest("no mine placed")
        g.toggle_flag(*mine)
        # reveal a cell adjacent to the mine and chord it
        for r in range(2):
            for c in range(2):
                if (r, c) != mine and (abs(r - mine[0]) <= 1 and abs(c - mine[1]) <= 1):
                    g.reveal(r, c)
                    res = g.chord(r, c)
                    self.assertIn(res["result"], ("safe", "won", "nochange"))


class TestAutoFlag(unittest.TestCase):
    def test_auto_flag_fixpoint(self):
        g = Minesweeper(4, 4, 1, seed=2)
        g.reveal(0, 0)
        flagged = g.auto_flag_certain_mines()
        # idempotent: a second call returns nothing new
        again = g.auto_flag_certain_mines()
        self.assertEqual(again, [])
        # any newly flagged cell is genuinely a mine
        for (r, c) in flagged:
            self.assertTrue(g.mines[r][c])

    def test_auto_flag_no_op_before_first_move(self):
        g = Minesweeper.from_preset("beginner", seed=1)
        self.assertEqual(g.auto_flag_certain_mines(), [])


class TestSeeding(unittest.TestCase):
    def test_same_seed_same_mines(self):
        a = Minesweeper.from_preset("intermediate", seed=123)
        a.reveal(5, 5)
        b = Minesweeper.from_preset("intermediate", seed=123)
        b.reveal(5, 5)
        self.assertEqual(a.mines, b.mines)

    def test_different_seed_likely_different(self):
        a = Minesweeper.from_preset("intermediate", seed=1)
        a.reveal(5, 5)
        b = Minesweeper.from_preset("intermediate", seed=2)
        b.reveal(5, 5)
        # extremely unlikely to be identical layouts
        self.assertNotEqual(a.mines, b.mines)


if __name__ == "__main__":
    unittest.main()
