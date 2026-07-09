"""Persistent run history for AI Minesweeper.

Each finished game is appended as one JSON object per line (JSONL) to
`run_history.jsonl` in the working directory. This lets us evaluate how
well a given model/difficulty configuration performs over many games.
"""
import json
import os
import time


HISTORY_PATH = "run_history.jsonl"


def record(result, *, difficulty, width, height, num_mines,
            model, provider, moves, revealed, duration_s,
            seed=None, note=None):
    """Append one finished-game record. Returns the record dict (also written).

    result: "won" | "lost" | "stopped"
    moves:   number of executed tool actions
    revealed: number of safe cells revealed at game end
    duration_s: wall-clock seconds elapsed for the game
    """
    rec = {
        "ts": int(time.time()),
        "result": result,
        "difficulty": difficulty,
        "size": f"{width}x{height}",
        "mines": num_mines,
        "model": model,
        "provider": provider,
        "moves": moves,
        "revealed": revealed,
        "duration_s": round(duration_s, 3),
        "seed": seed,
    }
    if note:
        rec["note"] = note
    _append(rec)
    return rec


def _append(rec):
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        # Never let history logging break gameplay.
        pass


def load_all():
    """Return list of all recorded game dicts (oldest first)."""
    if not os.path.exists(HISTORY_PATH):
        return []
    out = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return out
    return out


def summarize(records=None):
    """Return a short text summary of win rate and averages over records."""
    records = records if records is not None else load_all()
    if not records:
        return "尚无对局记录。"
    n = len(records)
    won = sum(1 for r in records if r.get("result") == "won")
    lost = sum(1 for r in records if r.get("result") == "lost")
    stopped = n - won - lost
    avg_moves = sum((r.get("moves", 0) for r in records)) / n
    avg_dur = sum((r.get("duration_s", 0) for r in records)) / n
    return (
        f"对局 {n} 局 | 胜 {won} ({won*100//n}%) | "
        f"负 {lost} | 中止 {stopped} | "
        f"平均动作 {avg_moves:.1f} | 平均用时 {avg_dur:.1f}s"
    )
