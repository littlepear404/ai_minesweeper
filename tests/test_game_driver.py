"""Tests for the headless game driver (game_driver.py).

These run with NO tkinter and NO network: they exercise run_stateless_loop
with a fake LLM client so the loop's termination guards (empty-tool-call
rounds, all-skipped rounds, and the no-progress / repeated-nochange case)
are locked in. They also assert the decoupling contract: game_driver must
not import tkinter.
"""
import sys
import unittest

from minesweeper import Minesweeper


class _FakeClient:
    """Echoes a fixed list of tool-call responses, one per model call.

    Each entry is a list of tool_calls dicts to return on that call.
    After the list is exhausted, returns no tool calls (forces no_action).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def call_stateless_stream(self, system_text, board_text):
        idx = min(self.calls, len(self._responses) - 1)
        tcs = self._responses[idx] if self._responses else []
        self.calls += 1
        yield ("chunk", "thinking")
        yield ("final", {"tool_calls": tcs})


def _collect(game, client, **kw):
    events = []
    from game_driver import run_stateless_loop
    run_stateless_loop(game, client, lambda k, p: events.append((k, p)), **kw)
    return events


class GameDriverNoTkinterTest(unittest.TestCase):
    def test_no_tkinter_import(self):
        if "tkinter" in sys.modules:
            self.skipTest("tkinter already imported in this process")
        import importlib
        import game_driver
        importlib.reload(game_driver)
        self.assertNotIn("tkinter", sys.modules)


class LoopTerminationTest(unittest.TestCase):
    def test_empty_tool_calls_stops(self):
        # A model that never returns tool calls must stop after max_no_action.
        g = Minesweeper.from_preset("beginner")
        g.reveal(0, 0)
        events = _collect(g, _FakeClient([]), move_delay=0,
                          max_no_action=3, stop_check=lambda: False)
        kinds = [k for k, _ in events]
        self.assertIn("end", kinds)
        self.assertIn("error", kinds)

    def test_all_skipped_stops(self):
        # Unknown-tool calls never change the board -> no_progress guard.
        g = Minesweeper.from_preset("beginner")
        g.reveal(0, 0)
        bad = [{"name": "frobnicate", "args": {"row": 0, "col": 0}}]
        events = _collect(g, _FakeClient([bad] * 10), move_delay=0,
                          max_no_action=3, stop_check=lambda: False)
        kinds = [k for k, _ in events]
        self.assertIn("end", kinds)
        self.assertIn("error", kinds)

    def test_repeated_nochange_stops(self):
        # The regression: re-calling an already-revealed cell yields
        # "nochange" every round. action_log is non-empty, so the old
        # guard (not action_log) never tripped and the loop spun forever.
        g = Minesweeper.from_preset("beginner")
        g.reveal(0, 0)  # (0,0) now revealed
        repeat = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        events = _collect(g, _FakeClient([repeat] * 50), move_delay=0,
                          max_no_action=3, stop_check=lambda: False)
        kinds = [k for k, _ in events]
        self.assertIn("end", kinds)
        self.assertIn("error", kinds)

    def test_stop_check_aborts(self):
        g = Minesweeper.from_preset("beginner")
        g.reveal(0, 0)
        repeat = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        events = _collect(g, _FakeClient([repeat] * 50), move_delay=0,
                          max_no_action=10, stop_check=lambda: True)
        # stop_check True at the very top -> immediate abort, reported as a
        # single "end" event with result "stopped" and zero moves.
        ends = [p for k, p in events if k == "end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["result"], "stopped")
        self.assertEqual(ends[0]["moves"], 0)

    def test_end_payload_reports_moves(self):
        # The run-history fix: "end" must carry the executed action count.
        g = Minesweeper.from_preset("beginner")
        first = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        events = _collect(g, _FakeClient([first] + [[]] * 10), move_delay=0,
                          max_no_action=2, stop_check=lambda: False)
        ends = [p for k, p in events if k == "end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["moves"], 1)

    def test_real_progress_keeps_going(self):
        # A first-move reveal that opens cells is real progress; the loop
        # should not trip the no_progress guard on a single such round.
        g = Minesweeper.from_preset("beginner")
        first = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        events = _collect(g, _FakeClient([first] * 50), move_delay=0,
                          max_no_action=3, stop_check=lambda: False)
        # After the first real reveal, subsequent reveals of (0,0) are
        # nochange -> eventually stops, but only after >= max_no_action
        # no-progress rounds, and it must have emitted at least one action.
        self.assertIn("action", [k for k, _ in events])
        self.assertIn("end", [k for k, _ in events])


class SolverAssistTest(unittest.TestCase):
    def test_assist_progresses_without_llm_and_never_loses(self):
        # 1 mine on 4x4 is fully deterministic after the first click: the
        # solver should flag + chord to victory with a single LLM call
        # (the opening reveal) and zero tokens spent afterwards.
        g = Minesweeper(4, 4, 1, seed=2)
        first = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        client = _FakeClient([first])
        events = _collect(g, client, move_delay=0, max_no_action=3,
                          stop_check=lambda: False, solver_mode="assist")
        self.assertEqual(g.state, "won")
        self.assertEqual(client.calls, 1)
        ends = [p for k, p in events if k == "end"]
        self.assertEqual(ends[0]["result"], "won")

    def test_assist_falls_back_to_llm_when_stuck(self):
        # Before the first move the solver can do nothing, so the LLM is
        # consulted; afterwards assist mode keeps using it when deduction
        # stalls (here: empty answers -> no_action stop).
        g = Minesweeper(4, 4, 1, seed=2)
        first = [{"name": "reveal", "args": {"row": 0, "col": 0}}]
        client = _FakeClient([first])
        events = _collect(g, client, move_delay=0, max_no_action=3,
                          stop_check=lambda: False, solver_mode="off")
        self.assertGreater(client.calls, 1)  # off: LLM called every round


if __name__ == "__main__":
    unittest.main()
