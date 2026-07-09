# AGENTS.md

Repository: AI Minesweeper — a Tkinter desktop app where an LLM (OpenAI- or
Anthropic-compatible API) plays classic Minesweeper autonomously via tool calls.

## Run

```bash
pip install -r requirements.txt        # only `requests` beyond stdlib
python main.py                          # launches the Tkinter GUI
```

No build step, no tests yet. Verify changes with `python -m py_compile
minesweeper.py llm_client.py gui.py main.py` and the smoke check below.

## Architecture

- `minesweeper.py` — pure game logic. No I/O, no Tkinter. First click is
  always safe (3x3 safe zone generated on first reveal via `_place_mines`).
  Board state machine: `ready -> playing -> won|lost`. Use `Minesweeper.from_preset(name)`; presets live in `DIFFICULTIES` (beginner/intermediate/expert).
- `gui.py` — Tkinter UI + the LLM driver thread. **Two worker paths**:
  - `_run_loop_stateless` (default, for "开始/重启"): each LLM call sends
    *only* `SYSTEM_PROMPT` + current board snapshot — no history. The model
    may return a batch of 1-5 tool_calls; the program executes them
    sequentially, re-validating each against the latest board state. Actions
    are interrupted on: `nochange`, single-cell reveal (≤1 opened, heuristic
    for guessing), game over, or `invalid`/`over`. Auto-flag runs after each
    reveal/chord. A 1-2 line `last_round_summary` is appended to the next
    snapshot so the LLM knows which actions were executed/skipped.
  - `_run_loop_stateful` (for "继续"): legacy path that uses `client.turn_stream`,
    `add_tool_result`, and `trim_history`. Preserves conversation context
    across the paused game.
  Both post `(kind, payload)` tuples to `self.cmd_queue`; the main thread
  polls it every 100ms via `_poll_queue`/`_handle_cmd`.
- `llm_client.py` — thin HTTP client. Three call styles:
  - `turn()` / `turn_stream()` — stateful (append to `self.history`).
  - `call_stateless_stream(system, board)` — builds fresh `messages=[system, user]`
    every call, does NOT read/write `self.history`. Used by stateless worker.
  Supports `provider: openai` (also sends `tool_temperature` when configured)
  and `provider: anthropic`. Tools are `reveal`, `toggle_flag`, and `chord`
  (defined in `COMMON_TOOLS`). `_should_omit_tool_choice` handles DeepSeek-V4
  reasoning models that reject the `tool_choice` parameter.
- `main.py` — entrypoint, just import-guards Tkinter/requests then calls `gui.main()`.
- `llm_config.json` — **the only** API config (key, base_url, model, tempo).
  Tracked in git with a placeholder key; real keys are secrets and must never
  be committed. `keep_recent_turns`, `move_delay`, `max_no_action_retries`
  tune the game loop.

## Conventions that matter

- Threading: do not call Tk widgets from `_run_loop`. Push a queue command.
  `time.sleep(self.move_delay)` between actions is intentional so the user can
  follow the game.
- Tools: the LLM gets exactly three functions — `reveal(row, col)`,
  `toggle_flag(row, col)`, and `chord(row, col)`. `chord` opens all unflagged
  hidden neighbours of a revealed numbered cell when its flagged-neighbour
  count equals its number (classic minesweeper double-click). The program
  validates chord legality (flag count == number) before executing.
  Coordinates are **0-indexed**; the system prompt states this and the board
  text snapshot labels rows/cols. Don't add 1.
- Board text (`Minesweeper.to_text`) is the LLM's whole view of the game; it
  hides unrevealed cells as `.` and flags as `F`. If you change the snapshot
  format, also update `SYSTEM_PROMPT` in `gui.py`.
- Auto-flag: `Minesweeper.auto_flag_certain_mines()` runs after every
  `reveal`/`chord`. It flags any hidden neighbours of a number cell N where
  `hidden+flagged == N` (iterate to fixpoint). The LLM is informed of this
  via the system prompt; new `F`s appear in the next board snapshot.
- First-move safety is implemented lazily — mines are placed *after* the first
  `reveal`, excluding a 3x3 area around the clicked cell. Don't call
  `_place_mines` directly.
- Windows is the dev platform; the console may mojibake Chinese from stdlib
  print output — that's a console codepage artifact, not a file bug. Files are
  UTF-8.
- Stopping mid-game: `on_stop` flips `self.running=False`; the worker checks
  this between actions. Don't block the main thread on the worker.

## Smoke check (no API key needed)

```bash
python -c "from minesweeper import Minesweeper; g=Minesweeper.from_preset('beginner'); r=g.reveal(0,0); print(r['result'], r['cells'][:3], g.state)"
python -c "from llm_client import build_openai_tools, build_anthropic_tools; print(len(build_openai_tools()), len(build_anthropic_tools()))"
```

First should print `safe ... playing`; both tool counts should be `3`.

## Config fields (llm_config.json)

`provider` (`openai`|`anthropic`), `api_base_url`, `api_key`, `model`,
`temperature`, `max_tokens`, `request_timeout`, `move_delay` (seconds between
LLM actions), `keep_recent_turns` (history window, 0 = keep all),
`max_no_action_retries` (stop after N consecutive no-call turns). `cell_size`
(optional, px) also lives here. `tool_temperature` (optional, sent as-is for
OpenAI-compatible providers that support it; lower values bias towards tool
use).

## Known limits / next steps

- No persistent scoring or run history.
- Tests live in `tests/` (`test_minesweeper.py`, `test_llm_client.py`); run with
  `python -m unittest tests.test_minesweeper tests.test_llm_client`.
  Covers game logic plus `llm_client` tool-result formatting, SSE chunk
  detection, streaming parse, and history trimming (mocked `requests`).
- LLM debugging requires a real API key in `llm_config.json` — deferred until
  the user provides one. With the placeholder key, `LLMClient.__init__` raises
  `LLMError`, so "开始/重启" shows a config-error dialog (expected).