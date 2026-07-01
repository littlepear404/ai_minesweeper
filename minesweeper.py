"""Minesweeper game logic.

Difficulty presets:
  beginner     : 9x9,  10 mines
  intermediate : 16x16, 40 mines
  expert       : 30x16, 99 mines
"""
import random

DIFFICULTIES = {
    "beginner":     {"width": 9,  "height": 9,  "mines": 10},
    "intermediate": {"width": 16, "height": 16, "mines": 40},
    "expert":       {"width": 30, "height": 16, "mines": 99},
}

NUMBER_COLORS = {
    0: "#c0c0c0",
    1: "#0000ff",
    2: "#008000",
    3: "#ff0000",
    4: "#000080",
    5: "#800000",
    6: "#008080",
    7: "#000000",
    8: "#808080",
}


class Minesweeper:
    def __init__(self, width, height, mines, seed=None):
        self.width = width
        self.height = height
        self.num_mines = mines
        self.state = "ready"  # ready -> playing -> won/lost
        self._rng = random.Random(seed)

        self.mines = [[False] * width for _ in range(height)]
        self.numbers = [[0] * width for _ in range(height)]
        self.revealed = [[False] * width for _ in range(height)]
        self.flagged = [[False] * width for _ in range(height)]
        self.explode_cell = None
        self.first_move_done = False
        self.revealed_count = 0

    @classmethod
    def from_preset(cls, name, seed=None):
        cfg = DIFFICULTIES[name]
        return cls(cfg["width"], cfg["height"], cfg["mines"], seed=seed)

    def _in_bounds(self, r, c):
        return 0 <= r < self.height and 0 <= c < self.width

    def _neighbors(self, r, c):
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if self._in_bounds(nr, nc):
                    yield nr, nc

    def _place_mines(self, safe_r, safe_c):
        forbidden = {(safe_r, safe_c)}
        for nr, nc in self._neighbors(safe_r, safe_c):
            forbidden.add((nr, nc))
        positions = []
        for r in range(self.height):
            for c in range(self.width):
                if (r, c) not in forbidden:
                    positions.append((r, c))
        self._rng.shuffle(positions)
        for r, c in positions[: self.num_mines]:
            self.mines[r][c] = True
        for r in range(self.height):
            for c in range(self.width):
                self.numbers[r][c] = sum(
                    1 for nr, nc in self._neighbors(r, c) if self.mines[nr][nc]
                )

    def reveal(self, r, c):
        """Reveal a cell. Returns dict describing what happened.

        result: 'safe' (maybe with flood open), 'mine' (lost), 'nochange'
        """
        if not self._in_bounds(r, c):
            return {"result": "invalid", "message": f"坐标越界: ({r},{c})"}
        if self.state == "lost" or self.state == "won":
            return {"result": "over", "message": "游戏已结束"}
        if self.flagged[r][c]:
            return {"result": "nochange", "message": f"({r},{c}) 已标记，无法翻开"}
        if self.revealed[r][c]:
            return {"result": "nochange", "message": f"({r},{c}) 已翻开"}

        if not self.first_move_done:
            self._place_mines(r, c)
            self.first_move_done = True
            self.state = "playing"

        if self.mines[r][c]:
            self.revealed[r][c] = True
            self.explode_cell = (r, c)
            self.state = "lost"
            self._reveal_all_mines()
            return {"result": "mine", "cell": (r, c)}

        newly = self._flood_reveal(r, c)
        if self._check_win():
            self.state = "won"
            self._flag_all_mines_on_win()
            return {"result": "won", "cells": newly}
        return {"result": "safe", "cells": newly}

    def _flood_reveal(self, r, c):
        opened = []
        stack = [(r, c)]
        while stack:
            cr, cc = stack.pop()
            if not self._in_bounds(cr, cc):
                continue
            if self.revealed[cr][cc] or self.flagged[cr][cc] or self.mines[cr][cc]:
                continue
            self.revealed[cr][cc] = True
            self.revealed_count += 1
            opened.append((cr, cc))
            if self.numbers[cr][cc] == 0:
                for nr, nc in self._neighbors(cr, cc):
                    if not (self.revealed[nr][nc] or self.flagged[nr][nc]):
                        stack.append((nr, nc))
        return opened

    def toggle_flag(self, r, c):
        if not self._in_bounds(r, c):
            return {"result": "invalid", "message": f"坐标越界: ({r},{c})"}
        if self.state == "lost" or self.state == "won":
            return {"result": "over", "message": "游戏已结束"}
        if self.revealed[r][c]:
            return {"result": "nochange", "message": f"({r},{c}) 已翻开，无法插旗"}
        if not self.first_move_done:
            # allow flagging even before first reveal (no mine placement needed)
            self.state = "playing"
        self.flagged[r][c] = not self.flagged[r][c]
        return {"result": "flag" if self.flagged[r][c] else "unflag", "cell": (r, c)}

    def chord(self, r, c):
        """Chord (double-click) a revealed numbered cell.

        If that cell's number equals the count of its flagged neighbors,
        open every hidden, *unflagged* neighbor at once. If any opened cell
        is a mine => lose (false flags cause loss, just like a human chording).

        Returns dict:
          result: 'nochange' (not a number / not revealed / flagged!=n),
                  'safe' (opened n neighbors, no mine), 'mine' (hit a mine ->
                  lost), 'won', 'invalid'.
          cells: list of newly opened coords when safe/won.
        """
        if not self._in_bounds(r, c):
            return {"result": "invalid", "message": f"坐标越界: ({r},{c})"}
        if self.state == "lost" or self.state == "won":
            return {"result": "over", "message": "游戏已结束"}
        if not self.revealed[r][c] or self.mines[r][c]:
            return {"result": "nochange", "message": f"({r},{c}) 未翻开或是雷"}
        n = self.numbers[r][c]
        if n == 0:
            return {"result": "nochange", "message": f"({r},{c}) 为 0 格，无需双击"}
        flagged_count = sum(1 for nr, nc in self._neighbors(r, c) if self.flagged[nr][nc])
        if flagged_count != n:
            return {"result": "nochange",
                    "message": f"({r},{c}) 周围旗数 {flagged_count} != 数字 {n}"}

        opened = []
        for nr, nc in self._neighbors(r, c):
            if not self.revealed[nr][nc] and not self.flagged[nr][nc]:
                if self.mines[nr][nc]:
                    self.revealed[nr][nc] = True
                    self.explode_cell = (nr, nc)
                    self.state = "lost"
                    self._reveal_all_mines()
                    return {"result": "mine", "cell": (nr, nc)}
                opened.extend(self._flood_reveal(nr, nc))

        if self._check_win():
            self.state = "won"
            self._flag_all_mines_on_win()
            return {"result": "won", "cells": opened}
        return {"result": "safe", "cells": opened}

    def _reveal_all_mines(self):
        for r in range(self.height):
            for c in range(self.width):
                if self.mines[r][c]:
                    self.revealed[r][c] = True

    def _flag_all_mines_on_win(self):
        for r in range(self.height):
            for c in range(self.width):
                if self.mines[r][c]:
                    self.flagged[r][c] = True

    def _check_win(self):
        total_cells = self.width * self.height
        return self.revealed_count == total_cells - self.num_mines

    def auto_flag_certain_mines(self):
        """Scan revealed numbered cells; for each, if flags + hidden ==
        number (and hidden>0), all those hidden neighbors are certainly
        mines -> flag them. Iterate to fixpoint. Returns list of newly
        flagged (r, c) (order of discovery, deduped).
        """
        if self.state in ("won", "lost") or not self.first_move_done:
            return []
        seen = set()
        newly = []
        changed = True
        guard = 0
        while changed and guard < 64:
            changed = False
            guard += 1
            for r in range(self.height):
                for c in range(self.width):
                    if not self.revealed[r][c] or self.mines[r][c]:
                        continue
                    n = self.numbers[r][c]
                    if n == 0:
                        continue
                    hidden = []
                    flagged = 0
                    for nr, nc in self._neighbors(r, c):
                        if self.revealed[nr][nc]:
                            continue
                        if self.flagged[nr][nc]:
                            flagged += 1
                        else:
                            hidden.append((nr, nc))
                    if hidden and (flagged + len(hidden)) == n:
                        for (hr, hc) in hidden:
                            self.flagged[hr][hc] = True
                            if (hr, hc) not in seen:
                                seen.add((hr, hc))
                                newly.append((hr, hc))
                            changed = True
        return newly

    def cell_symbol(self, r, c):
        if self.revealed[r][c]:
            if self.mines[r][c]:
                return "*"
            return str(self.numbers[r][c])
        return "F" if self.flagged[r][c] else "."

    def to_text(self, hide_unrevealed=False):
        """Return a coordinate-labeled grid for the LLM.

        Legend: '.' hidden  'F' flagged  digits 0-8 revealed counts  '*' mine
        """
        lines = []
        cw = max(2, len(str(self.width - 1)) + 1)
        label_w = max(2, len(str(self.height - 1)))
        header = " " * (label_w + 1) + "".join(str(c).rjust(cw) for c in range(self.width))
        lines.append(header)
        for r in range(self.height):
            row = str(r).rjust(label_w) + " "
            parts = []
            for c in range(self.width):
                if self.revealed[r][c]:
                    parts.append(str(self.numbers[r][c]).rjust(cw))
                else:
                    parts.append(("F" if self.flagged[r][c] else ".").rjust(cw))
            lines.append(row + "".join(parts))
        return "\n".join(lines)

    def summary(self):
        total = self.width * self.height
        return (
            f"已翻开 {self.revealed_count}/{total - self.num_mines} 个安全格, "
            f"已标记 {sum(row.count(True) for row in self.flagged)}/{self.num_mines} 雷, "
            f"状态: {self.state}"
        )