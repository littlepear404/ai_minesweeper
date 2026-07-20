# AGENTS.md

Repository: AI Minesweeper — a Tkinter desktop app where an LLM (OpenAI- or
Anthropic-compatible API) plays classic Minesweeper autonomously via tool calls.

## Run

```bash
pip install -r requirements.txt        # only `requests` beyond stdlib
python main.py                          # launches the Tkinter GUI
```

No build step. Verify changes with `python -m py_compile
minesweeper.py llm_client.py gui.py main.py` and the smoke check below.
Tests live in `tests/`; run the full suite with
`python -m unittest tests.test_minesweeper tests.test_llm_client tests.test_run_history tests.test_game_driver`.

## Architecture

- `minesweeper.py` — pure game logic. No I/O, no Tkinter. First click is
  always safe (3x3 safe zone generated on first reveal via `_place_mines`).
  Board state machine: `ready -> playing -> won|lost`. Use `Minesweeper.from_preset(name)`; presets live in `DIFFICULTIES` (beginner/intermediate/expert).
  Also hosts the deterministic local solver: `auto_flag_certain_mines()`
  (flag provable mines) and `auto_chord_certain_safe()` (chord provably
  satisfied numbers). Both only trust flags in `self.certain_flags` — flags
  placed via `toggle_flag` (LLM/manual guesses) never seed deductions, so
  the solver is sound by induction and can never explode on a mis-flag.
- `game_driver.py` — **tkinter-free** headless game loop. Holds
  `SYSTEM_PROMPT`, `load_config()`, `_format_tool_result()`, `_solver_step()`,
  and the core `run_stateless_loop(game, client, emit, *, move_delay,
  max_no_action, stop_check, system_prompt, solver_mode)`.
  `emit(kind, payload)` is the single integration point; GUI and CLI are
  just different `emit` adapters. With `solver_mode="assist"` each round
  first runs `_solver_step` to fixpoint; while local deduction progresses
  the LLM is NOT called (zero token cost) — the model is only consulted
  when a guess or higher-level reasoning is needed. The loop terminates on:
  empty tool-call rounds (`no_action`), all-skipped/no-progress rounds
  (`no_progress` — covers the repeated-`nochange` infinite-loop trap),
  game over, or `stop_check()` returning True. Every exit emits
  `("end", {"result", "moves", "input_tokens", "output_tokens"})` — result
  is `won|lost|stopped|error`. Per-round token usage is emitted as
  `("usage", {...})`. Keep `SYSTEM_PROMPT` in sync with
  `Minesweeper.to_text_compact()`'s snapshot format.
- `cli.py` — headless entry point (`python -m cli`) for debugging the LLM
  loop without the GUI. Adapts `emit` to stdout and Ctrl-C to `stop_check`.
  Batch evaluation mode: `python -m cli -d intermediate --games 20
  --seed-start 1000` plays N games quietly, records each to
  `run_history.jsonl`, and prints a win-rate / token aggregate.
- `gui.py` — Tkinter UI + the LLM driver thread. Single worker path:
  `_run_loop_stateless` delegates to `run_stateless_loop(...)`, passing
  `self._put` as `emit` and `lambda: not self.running` as `stop_check`.
  "继续" simply re-enters the same loop with the live game object (the
  loop is stateless, so the next snapshot carries the full board).
  The worker posts `(kind, payload)` tuples to `self.cmd_queue`; the main
  thread polls it every 100ms via `_poll_queue`/`_handle_cmd`.
- `llm_client.py` — thin HTTP client, **stateless only**:
  `call_stateless_stream(system, board)` builds fresh
  `messages=[system, user]` every call. Supports `provider: openai`
  (sends `tool_temperature` / `reasoning_effort` when configured, requests
  `stream_options.include_usage`) and `provider: anthropic` (marks system
  prompt + tools with `cache_control: ephemeral` so cached input tokens
  bill at ~1/10 price). Tools are `reveal`, `toggle_flag`, and `chord`
  (defined in `COMMON_TOOLS`, descriptions deliberately terse — the system
  prompt carries the detail). `_should_omit_tool_choice` handles
  DeepSeek-V4 reasoning models that reject the `tool_choice` parameter.
- `main.py` — entrypoint, just import-guards Tkinter/requests then calls `gui.main()`.
- `llm_config.json` — **the only** API config (key, base_url, model, tempo).
  Git-ignored (only `llm_config.template.json` is tracked); real keys are
  secrets and must never be committed. `move_delay`,
  `max_no_action_retries`, `solver_mode` tune the game loop.

## Conventions that matter

- Threading: do not call Tk widgets from `_run_loop`. Push a queue command.
  `time.sleep(self.move_delay)` between actions is intentional so the user can
  follow the game.
- Tools: the LLM gets exactly three functions — `reveal(row, col)`,
  `toggle_flag(row, col)`, and `chord(row, col)`. `chord` opens all unflagged
  hidden neighbours of a revealed numbered cell when its flagged-neighbour
  count equals its number (classic minesweeper double-click). The program
  validates chord legality (flag count == number) before executing.
  Coordinates are **0-indexed**; the system prompt states this. Don't add 1.
- Board text: the stateless loop sends `Minesweeper.to_text_compact()` (a
  label-free per-row string, ~69% fewer tokens than `to_text()` on expert)
  as the LLM's whole view of the game; unrevealed = `.`, flags = `F`,
  revealed numbers 0-8. `summary()` carries the row/col totals for
  coordinate lookup. `SYSTEM_PROMPT` lives in `game_driver.py` (kept deliberately
  terse (~half the original token cost) — if you change the snapshot
  format, keep `SYSTEM_PROMPT`'s board description in sync.
- Auto-flag / auto-chord (the local solver):
  `Minesweeper.auto_flag_certain_mines()` runs after every `reveal`/`chord`,
  and with `solver_mode="assist"` both it and `auto_chord_certain_safe()`
  run to fixpoint before every LLM call. They only trust flags in
  `certain_flags` (pure deduction); LLM/manual flags are guesses and never
  seed new deductions. The LLM is informed of auto-flag via the system
  prompt; new `F`s appear in the next board snapshot.
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
LLM actions), `max_no_action_retries` (stop after N consecutive no-call
turns), `solver_mode` (`assist` = local deterministic solver plays provable
moves for free, LLM only called when stuck; `off` = LLM plays every step).
`cell_size` (optional, px) also lives here. `tool_temperature` (optional,
sent as-is for OpenAI-compatible providers that support it; lower values
bias towards tool use). `reasoning_effort` (optional, e.g. `"low"`;
pass-through for OpenAI reasoning models to cut thinking-token cost —
only sent when explicitly configured since strict servers may reject it).

## Known limits / next steps

- Run history is persisted to `run_history.jsonl` (one JSON record per
  finished game: difficulty, size, model, provider, moves, revealed
  count, duration, seed, and input/output token totals when the provider
  reports usage). The "查看战绩" button shows a rolling
  win-rate/average summary via `run_history.summarize()`.
- Tests live in `tests/`; run the full suite with
  `python -m unittest tests.test_minesweeper tests.test_llm_client tests.test_run_history tests.test_game_driver`.
  Covers game logic, solver soundness (certain-flag induction, wrong-flag
  auto-chord guard), `llm_client` SSE chunk detection / streaming parse /
  usage extraction / request-body shape (mocked `requests`), run-history
  JSONL persistence/summary, and `game_driver` loop-termination guards
  (including the repeated-`nochange` infinite-loop regression) plus
  solver-assist behaviour.
- The game loop is decoupled from the GUI: `python -m cli` runs the same
  `run_stateless_loop` headlessly (no Tkinter) so the LLM's behaviour, token
  usage, and tool-call parsing can be debugged from a terminal; `--games N`
  gives a batch benchmark harness.
- LLM debugging requires a real API key in `llm_config.json` — deferred until
  the user provides one. With the placeholder key, `LLMClient.__init__` raises
  `LLMError`, so "开始/重启" shows a config-error dialog (expected).