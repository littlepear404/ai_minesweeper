"""AI Minesweeper GUI.

Layout:
  Left  : minesweeper board canvas + status bar + control buttons
  Right : scrollable panel showing the LLM's thinking and actions

The LLM runs in a background thread. The board is updated on the main thread
via a thread-safe command queue. A move delay between turns lets the user
follow what the model is doing.
"""
import ctypes
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from minesweeper import Minesweeper, DIFFICULTIES, NUMBER_COLORS
from llm_client import LLMClient, LLMError
from run_history import record as record_run, summarize as summarize_runs

UI_FONT_SIZE = 14
THINK_FONT_SIZE = 14
BOARD_TEXT_FONT = "Consolas"


CONFIG_PATH = "llm_config.json"

SYSTEM_PROMPT = """经典扫雷: 翻开所有非雷格即获胜, 踩雷即输。
坐标 row/col 均从0开始(左上角0,0)。

棋盘为紧凑文本(每行一串, 行从上到下、列从左到右, 索引0):
  '.' 未翻开   'F' 你插的旗(疑雷)   0-8 已翻开格的周围雷数   '*' 雷(仅输/赢时出现)
棋盘末行"已翻开 X/Y 安全格"给出总行/列数, 供你定位 (row,col)。

你是"当前局面动作建议器"(非多步规划器), 不要预判某步之后的新信息。
每轮: 1) 用 ≤2-3 句简体中文极简说明推理要点(哪些格确定安全/是雷/可双击), 勿逐格列棋盘; 2) 紧跟 1~5 个工具调用。

工具(坐标0-index):
- reveal(r,c): 翻开。雷=>输; 数字=>显示周围雷数; 0格=>自动展开相邻安全区。
- toggle_flag(r,c): 切换未翻开格的旗(标记/取消疑雷)。
- chord(r,c): 对已翻开数字格双击; 当周围旗数==该数字时, 一次性翻开周围未翻开未插旗格; 旗标错则踩雷输。

程序会顺序执行你的工具列表, 每步后用最新棋盘重校验; 触发胜利/踩雷/大面积展开或判定为猜测即自动停后续。自动插旗: 数字N周围(未翻开+已插旗)==N 时必为雷, 直接标'F'。首点保护: 第一步永不踩雷(周围3x3无雷), 可选中心附近。

策略: 1) 优先 reveal/chord 确定性安全格, 旗数==数字且有未翻格时必用 chord(最省步骤)。 2) 数字N周围恰N个未翻格即雷->插旗。 3) 无确定格时选雷数约束最松处, 避大数字硬猜。 4) 猜测/单格试探本轮只出这一个动作。 5) 纯逻辑无法推进才概率猜并简述。
上轮若有动作被跳过, 棋盘下方"上轮结果"会告知, 据此调整。"""



def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("AI 扫雷 - LLM 自动玩")
        self.cfg = load_config()
        self.cell_size = self.cfg.get("cell_size", 32)
        self.move_delay = self.cfg.get("move_delay", 0.6)
        self.keep_recent = self.cfg.get("keep_recent_turns", 30)
        self.max_no_action = self.cfg.get("max_no_action_retries", 10)

        self._apply_fonts()

        self.difficulty_var = tk.StringVar(value="beginner")
        self.seed_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="点击\"开始/重启\"开始一局")
        self.thinking_status_var = tk.StringVar(value="空闲")
        self.running = False

        self._build_layout()
        self.game = None
        self.client = None
        self.cmd_queue = queue.Queue()
        self.worker = None

    def _apply_fonts(self):
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=UI_FONT_SIZE)
            except Exception:
                pass

    # -------------------------- UI -------------------------- #
    def _build_layout(self):
        top = ttk.Frame(self.root)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        ttk.Label(top, text="难度:").pack(side=tk.LEFT)
        for name in DIFFICULTIES:
            ttk.Radiobutton(top, text=name, value=name,
                            variable=self.difficulty_var).pack(side=tk.LEFT)
        ttk.Label(top, text="种子(可空):").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Entry(top, textvariable=self.seed_var, width=8).pack(side=tk.LEFT)

        self.start_btn = ttk.Button(top, text="开始/重启", command=self.on_start)
        self.start_btn.pack(side=tk.LEFT, padx=8)
        self.stop_btn = ttk.Button(top, text="停止", command=self.on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)
        self.continue_btn = ttk.Button(top, text="继续", command=self.on_continue, state=tk.DISABLED)
        self.continue_btn.pack(side=tk.LEFT, padx=8)
        self.edit_btn = ttk.Button(top, text="编辑配置", command=self.on_edit_config)
        self.edit_btn.pack(side=tk.LEFT, padx=8)
        self.stats_btn = ttk.Button(top, text="查看战绩", command=self.on_show_stats)
        self.stats_btn.pack(side=tk.LEFT, padx=8)
        self.export_btn = ttk.Button(top, text="导出日志", command=self.on_export_log)
        self.export_btn.pack(side=tk.LEFT, padx=8)

        body = ttk.Frame(self.root)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=6, pady=4)

        # Left: board (scrollable so expert 30x16 boards stay reachable)
        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False)
        board_frame = ttk.Frame(left)
        board_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(board_frame, bg="#bdbdbd", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(board_frame, orient=tk.VERTICAL,
                             command=self.canvas.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb = ttk.Scrollbar(board_frame, orient=tk.HORIZONTAL,
                             command=self.canvas.xview)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.config(xscrollcommand=hsb.set, yscrollcommand=vsb.set)
        self.canvas.bind("<MouseWheel>", self._on_board_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_board_mousewheel_shift)
        status = ttk.Frame(left)
        status.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)

        # Right: thinking panel
        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))
        ttk.Label(right, text="LLM 思考过程").pack(anchor="w")
        self.thinking_status = ttk.Label(right, textvariable=self.thinking_status_var,
                                         font=(None, UI_FONT_SIZE, "bold"))
        self.thinking_status.pack(anchor="w")
        self.thinking = tk.Text(right, wrap=tk.WORD, state=tk.DISABLED,
                                bg="#1e1e1e", fg="#d4d4d4", insertbackground="#d4d4d4",
                                font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
        self.thinking.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(right, command=self.thinking.yview)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        self.thinking.config(yscrollcommand=sb.set)

    # -------------------------- lifecycle -------------------------- #
    def on_start(self):
        if self.running:
            return
        seed = self.seed_var.get().strip()
        seed = int(seed) if seed else None
        self.game = Minesweeper.from_preset(self.difficulty_var.get(), seed=seed)
        try:
            self.client = LLMClient(self.cfg)
        except LLMError as e:
            messagebox.showerror("配置错误", str(e))
            return
        # still init history for stateful fallback (continue button)
        self.client.reset(SYSTEM_PROMPT + "\n\n当前难度: " + self.difficulty_var.get() +
                           f"\n棋盘尺寸: {self.game.width}x{self.game.height}, " +
                           f"雷数: {self.game.num_mines}")
        self.game_start_ts = time.time()
        self.move_count = 0
        self.game_recorded = False

        self._clear_thinking()
        self._append_text(f"=== 新游戏开始: {self.difficulty_var.get()} " +
                          f"{self.game.width}x{self.game.height} 雷 {self.game.num_mines} "
                          f"(无状态模式) ===\n", tag="sys")
        self._draw_board()
        self._update_status()
        self._begin_worker(stateless=True)

    def on_continue(self):
        if self.running:
            return
        if self.game is None or self.game.state in ("won", "lost", "ready") or self.client is None:
            return
        # The stateful path keeps conversation history, but on_start reset it
        # to just the system prompt -- so a bare "继续" had no context of
        # the current board. Re-feed the live board snapshot as the first
        # user turn so the model actually continues this game.
        snapshot = (
            "继续当前对局。以下是当前棋盘状态(row/col 从0开始, '.' 未翻开, "
            "'F' 旗, 数字为已翻开雷数):\n" + self.game.to_text()
            + "\n" + self.game.summary()
        )
        self.client.history.append({"role": "user", "content": snapshot})
        self._append_text("\n[继续(有状态模式), 已载入当前棋盘]\n", tag="sys")
        self._begin_worker(stateless=False)

    def _begin_worker(self, stateless=True):
        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.continue_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        while not self.cmd_queue.empty():
            try:
                self.cmd_queue.get_nowait()
            except queue.Empty:
                break
        target = self._run_loop_stateless if stateless else self._run_loop_stateful
        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()
        self.root.after(100, self._poll_queue)

    def on_stop(self):
        self.running = False
        self.thinking_status_var.set("空闲")
        self._append_text("\n[已停止]\n", tag="sys")
        # stop permits continue; new game still allowed
        self.stop_btn.config(state=tk.DISABLED)
        if self.game is not None and self.game.state == "playing":
            self.continue_btn.config(state=tk.NORMAL)
            self._record_game("stopped")
        self.start_btn.config(state=tk.NORMAL)

    def on_show_stats(self):
        # Show a rolling summary of recorded games (run_history.jsonl).
        messagebox.showinfo("战绩统计", summarize_runs())

    def on_export_log(self):
        # Save the right-hand thinking log to a UTF-8 text file.
        content = self.thinking.get("1.0", tk.END).rstrip("\n")
        if not content.strip():
            messagebox.showinfo("导出日志", "当前没有可导出的内容。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
            title="导出思考日志",
            initialfile=f"llm_log_{self.difficulty_var.get()}.txt",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content + "\n")
            messagebox.showinfo("导出日志", f"已保存到:\n{path}")
        except OSError as e:
            messagebox.showerror("导出失败", str(e))

    def on_edit_config(self):
        path = filedialog.askopenfilename(
            initialdir=".", filetypes=[("JSON", "*.json"), ("All", "*.*")],
            title="选择 llm_config.json")
        if not path:
            return
        try:
            self.cfg = load_config(path)
        except Exception as e:
            messagebox.showerror("读取失败", str(e))
            return
        # Apply every tunable that can change at runtime; cell_size also
        # needs a redraw so the new board sizing takes effect.
        prev_cell_size = self.cell_size
        self.move_delay = self.cfg.get("move_delay", self.move_delay)
        self.keep_recent = self.cfg.get("keep_recent_turns", self.keep_recent)
        self.max_no_action = self.cfg.get("max_no_action_retries", self.max_no_action)
        self.cell_size = self.cfg.get("cell_size", self.cell_size)
        if self.cell_size != prev_cell_size and self.game is not None:
            self._draw_board()
        messagebox.showinfo("已加载", f"配置已加载:\n{path}")

    # -------------------------- drawing -------------------------- #
    def _draw_board(self):
        g = self.game
        cs = self.cell_size
        self.canvas.delete("all")
        w = g.width * cs
        h = g.height * cs
        self.canvas.config(width=w, height=h, scrollregion=(0, 0, w, h))
        for r in range(g.height):
            for c in range(g.width):
                x0, y0 = c * cs, r * cs
                x1, y1 = x0 + cs, y0 + cs
                if g.revealed[r][c]:
                    color = "#d0d0d0" if not g.mines[r][c] else "#ff7777"
                    self.canvas.create_rectangle(x0, y0, x1, y1, fill=color,
                                                 outline="#9a9a9a")
                    sym = g.cell_symbol(r, c)
                    if sym == "*":
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2,
                                                text="\U0001F4A3", font=("Arial", int(cs*0.6)))
                    elif sym != "0":
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2, text=sym,
                                                fill=NUMBER_COLORS.get(int(sym), "#000"),
                                                font=("Consolas", int(cs*0.55), "bold"))
                else:
                    self.canvas.create_rectangle(x0, y0, x1, y1, fill="#bdbdbd",
                                                 outline="#9a9a9a")
                    if g.flagged[r][c]:
                        self.canvas.create_text((x0+x1)/2, (y0+y1)/2,
                                                text="\U0001F6A9", font=("Arial", int(cs*0.55)))
        if g.explode_cell:
            er, ec = g.explode_cell
            x0, y0 = ec*cs, er*cs
            self.canvas.create_rectangle(x0, y0, x0+cs, y0+cs, outline="red", width=3)

    def _update_status(self):
        g = self.game
        state_text = {"ready": "就绪", "playing": "进行中",
                      "won": "胜利!", "lost": "失败(踩雷)"}[g.state]
        self.status_var.set(
            f"{self.difficulty_var.get()} | {g.width}x{g.height} 雷 {g.num_mines} | "
            f"翻开 {g.revealed_count} | 状态: {state_text}"
        )

    def _on_board_mousewheel(self, event):
        # Vertical scroll on most mice; horizontal if Shift held on Windows.
        if event.state & 0x1:
            self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _on_board_mousewheel_shift(self, event):
        # Shift+Wheel maps to horizontal scroll on some platforms.
        self.canvas.xview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _record_game(self, result):
        # Persist a finished-game record so model quality can be evaluated.
        # Guard against double-recording (end handler + stop button).
        if self.game_recorded:
            return
        self.game_recorded = True
        if self.game is None or self.client is None:
            return
        try:
            duration = time.time() - self.game_start_ts
        except Exception:
            duration = 0.0
        record_run(
            result,
            difficulty=self.difficulty_var.get(),
            width=self.game.width, height=self.game.height,
            num_mines=self.game.num_mines,
            model=self.client.model, provider=self.client.provider,
            moves=self.move_count,
            revealed=self.game.revealed_count,
            duration_s=duration,
            seed=(int(self.seed_var.get()) if self.seed_var.get().strip() else None),
        )

    # -------------------------- thinking panel -------------------------- #
    def _clear_thinking(self):
        self.thinking.config(state=tk.NORMAL)
        self.thinking.delete("1.0", tk.END)
        self.thinking.config(state=tk.DISABLED)

    def _append_text(self, text, tag=None):
        self.thinking.config(state=tk.NORMAL)
        if tag:
            self.thinking.insert(tk.END, text, tag)
        else:
            self.thinking.insert(tk.END, text)
        self.thinking.see(tk.END)
        self.thinking.config(state=tk.DISABLED)

    # -------------------------- main thread poller -------------------------- #
    def _poll_queue(self):
        if not self.running and self.cmd_queue.empty():
            return
        try:
            while True:
                kind, payload = self.cmd_queue.get_nowait()
                self._handle_cmd(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _handle_cmd(self, kind, payload):
        if kind == "think_start":
            self.thinking_status_var.set("思考中...")
        elif kind == "think_chunk":
            self._append_text(payload, tag="think")
        elif kind == "think_end":
            self.thinking_status_var.set("空闲")
            if payload:
                self._append_text("\n", tag="think")
        elif kind == "thinking":
            self._append_text(payload + "\n", tag="think")
        elif kind == "action":
            self._append_text(payload + "\n", tag="act")
        elif kind == "result":
            self._append_text(payload + "\n", tag="res")
        elif kind == "error":
            self._append_text(payload + "\n", tag="err")
        elif kind == "redraw":
            self._draw_board()
            self._update_status()
        elif kind == "end":
            self.running = False
            self.thinking_status_var.set("游戏结束")
            self._record_game(self.game.state)
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.continue_btn.config(state=tk.DISABLED)
            self._draw_board()
            self._update_status()

    # -------------------------- LLM worker thread -------------------------- #
    def _run_loop_stateless(self):
        no_action = 0
        no_progress = 0  # rounds where the model returned calls but none executed
        last_summary = ""
        while self.running:
            g = self.game
            if g.state in ("won", "lost"):
                self._put("end", None)
                return
            hint = ""
            if not g.first_move_done:
                hint = "\n(第一步: 首点周围永远安全, 推荐中心附近)"
            snapshot = (
                "棋盘(每行一串, 行从上到下, 列从左到右, 索引0开始; "
                "'.' 未翻开, 'F' 旗, '0'-'8' 已翻开雷数):\n"
                + g.to_text_compact() + "\n" + g.summary()
                + hint + last_summary
            )
            if no_action > 0:
                snapshot += f"\n(上一轮未返回工具调用，连续空轮 {no_action} 次)"
            self._put("think_start", None)
            thinking = ""
            tool_calls = []
            try:
                for kind, val in self.client.call_stateless_stream(
                        SYSTEM_PROMPT, snapshot):
                    if not self.running:
                        self._put("think_end", None)
                        return
                    if kind == "chunk":
                        self._put("think_chunk", val)
                        thinking += val
                    elif kind == "final":
                        tool_calls = val.get("tool_calls") or []
            except LLMError as e:
                self._put("think_end", True)
                self._put("error", f"[LLM 调用失败] {e}")
                self._put("end", None)
                return
            self._put("think_end", True)
            self._put("think_chunk", "\n")

            if not tool_calls:
                no_action += 1
                self._put("result",
                          f"[模型未返回工具调用] 连续空轮 {no_action}")
                if no_action >= self.max_no_action:
                    self._put("error", "连续多次空轮，自动停止。")
                    self._put("end", None)
                    return
                continue
            no_action = 0

            # ------ batch execution ------
            action_log, skip_log = [], []
            for i, tc in enumerate(tool_calls):
                if not self.running:
                    return
                name = tc.get("name")
                args = tc.get("args") or {}
                row, col = args.get("row"), args.get("col")
                if row is None or col is None:
                    skip_log.append(f"{name}(?,?)原因:缺少参数")
                    self._put("result", f"[跳过] {name} 缺少参数")
                    break
                self._put("action", f">>> {name}(row={row}, col={col})")
                if name == "reveal":
                    out = g.reveal(row, col)
                elif name == "toggle_flag":
                    out = g.toggle_flag(row, col)
                elif name == "chord":
                    out = g.chord(row, col)
                else:
                    skip_log.append(f"{name}({row},{col})原因:未知工具")
                    self._put("result", f"[跳过] 未知工具: {name}")
                    break
                self._put("redraw", None)
                self.move_count += 1
                res = out.get("result")
                self._put("result", self._format_tool_result(out, name, row, col))
                autoflags = g.auto_flag_certain_mines() if name in ("reveal", "chord") else []
                if autoflags:
                    cells = ", ".join(f"({r},{c})" for r, c in autoflags)
                    self._put("result", f"  [自动插旗] {len(autoflags)} 格: {cells}")

                if res in ("invalid", "over"):
                    skip_log.append(f"{name}({row},{col})原因:{out.get('message','')}")
                    self._put("result", "[批量中断: 动作无效/游戏已结束]")
                    break

                action_log.append(f"{name}({row},{col})")

                if res == "mine":
                    break
                if res == "won":
                    action_log.append("胜利!")
                    break
                if res == "nochange":
                    self._put("result", f"[批量中断: {name} 无变化]")
                    break

                # heuristic: single-cell reveal = no structural progress (规则 7)
                if name == "reveal":
                    cells_opened = len(out.get("cells", []))
                    if cells_opened <= 1:
                        self._put("result", "[批量中断: 单格翻开, 后续动作可能基于旧信息]")
                        break

                time.sleep(self.move_delay)

            # round summary for next LLM call
            # A model that keeps returning tool calls which are all skipped
            # (invalid/unknown/over) never increments no_action, so count
            # those no-progress rounds separately to avoid an endless loop.
            if tool_calls and not action_log:
                no_progress += 1
            else:
                no_progress = 0
            if no_progress >= self.max_no_action:
                self._put("error", "连续多次返回无效/无执行动作，自动停止。")
                self._put("end", None)
                return
            parts = []
            if action_log:
                parts.append("上轮已执行: " + ", ".join(action_log))
            if skip_log:
                parts.append("上轮跳过: " + ", ".join(skip_log))
            last_summary = ("\n\n" + "\n".join(parts)) if parts else ""

            if g.state in ("won", "lost"):
                self._put("end", None)
                return

    def _run_loop_stateful(self):
        no_action = 0
        while self.running:
            g = self.game
            if g.state in ("won", "lost"):
                self._put("end", None)
                return
            remaining = max(self.max_no_action - no_action, 1)
            urgency = (
                f"\n\n运行约束: 本轮必须调用且只调用一个工具。"
                f"如果本轮不调用工具，将记为无动作；连续 {self.max_no_action} "
                f"次无动作会自动停止本局。当前已连续无动作 {no_action} 次，"
                f"本轮再无动作前剩余机会 {remaining} 次。"
                "请用简体中文简短说明，然后立即调用 reveal / toggle_flag / chord。"
            )
            snapshot = (
                "当前棋盘（row 为行，col 为列；从0开始；'.' 未翻开, 'F' 旗, 数字为已翻开雷数）:\n"
                + g.to_text() + "\n" + g.summary() + urgency
            )
            self._put("think_start", None)
            thinking = ""
            tool_calls = []
            try:
                for kind, val in self.client.turn_stream(snapshot):
                    if not self.running:
                        self._put("think_end", None)
                        return
                    if kind == "chunk":
                        self._put("think_chunk", val)
                        thinking += val
                    elif kind == "final":
                        thinking = val.get("thinking", thinking)
                        tool_calls = val.get("tool_calls") or []
            except LLMError as e:
                self._put("think_end", True)
                self._put("error", f"[LLM 调用失败] {e}")
                self._put("end", None)
                return
            self._put("think_end", True)
            # newline after the streamed thinking block
            self._put("think_chunk", "\n")

            if not tool_calls:
                no_action += 1
                self._put(
                    "result",
                    f"[模型未执行任何工具调用] 连续无动作 {no_action}/{self.max_no_action}",
                )
                if no_action >= self.max_no_action:
                    self._put("error", "连续多次未执行动作，自动停止。")
                    self._put("end", None)
                    return
                continue
            no_action = 0
            for tc in tool_calls:
                if not self.running:
                    return
                name = tc.get("name")
                args = tc.get("args") or {}
                row = args.get("row")
                col = args.get("col")
                if row is None or col is None:
                    self.client.add_tool_result(tc.get("id"), name or "unknown",
                                               "错误: 缺少 row 或 col 参数。")
                    self._put("result", "[跳过: 缺少参数]")
                    continue
                self._put("action", f">>> {name}(row={row}, col={col})")
                if name == "reveal":
                    out = g.reveal(row, col)
                    # auto-flag determined mines after a reveal
                    autoflags = g.auto_flag_certain_mines()
                elif name == "toggle_flag":
                    out = g.toggle_flag(row, col)
                    autoflags = []
                elif name == "chord":
                    out = g.chord(row, col)
                    # A successful chord can expose new numbered cells, which
                    # may in turn make additional mines certain.
                    autoflags = g.auto_flag_certain_mines()
                else:
                    out = {"result": "invalid", "message": f"未知函数: {name}"}
                    autoflags = []
                self._put("redraw", None)
                self._put("result", self._format_tool_result(out, name, row, col))
                if autoflags:
                    cell_strs = ", ".join(f"({r},{c})" for r, c in autoflags)
                    self._put("result", f"  [自动插旗] {len(autoflags)} 格: {cell_strs}")
                self.client.add_tool_result(
                    tc.get("id"), name,
                    self._tool_result_to_llm(out, name, row, col, autoflags))
                if g.state in ("won", "lost"):
                    self._put("end", None)
                    return
                self.client.trim_history(self.keep_recent)
                time.sleep(self.move_delay)

    @staticmethod
    def _format_tool_result(out, name, row, col):
        r = out.get("result")
        if r == "safe":
            n = len(out.get("cells", []))
            if name == "chord":
                return f"  -> 双击 ({row},{col}) 成功, 展开 {n} 格"
            return f"  -> 翻开 ({row},{col}) 成功, 展开 {n} 格"
        if r == "mine":
            if name == "chord":
                cell = out.get("cell", (row, col))
                return f"  -> 双击踩雷! {cell} 游戏失败"
            return f"  -> 踩雷! ({row},{col}) 游戏失败"
        if r == "won":
            return "  -> 胜利! 已翻开全部安全格"
        if r == "flag":
            return f"  -> 已在 ({row},{col}) 插旗"
        if r == "unflag":
            return f"  -> 已取消 ({row},{col}) 的旗"
        if r == "nochange":
            return f"  -> 无变化: {out.get('message','')}"
        if r == "invalid":
            return f"  -> 无效: {out.get('message','')}"
        if r == "over":
            return f"  -> 游戏已结束: {out.get('message','')}"
        return f"  -> {out}"

    @staticmethod
    def _tool_result_to_llm(out, name, row, col, autoflags=None):
        autoflags = autoflags or []
        extra = ""
        if autoflags:
            extra = f" 程序已为 {len(autoflags)} 个确定雷格自动插旗(见快照中的 'F')。"
        r = out.get("result")
        if r == "safe":
            verb = "双击" if name == "chord" else "翻开"
            return (f"{name}({row},{col}) 成功。{verb}后新打开了 "
                    f"{len(out.get('cells',[]))} 个格，棋盘已更新(见下次给你的快照)。"
                    + extra)
        if r == "mine":
            cell = out.get("cell", (row, col))
            return f"{name}({row},{col}) 触发了地雷 {cell}, 你输了这局。"
        if r == "won":
            return f"{name}({row},{col}) 之后你已胜利(翻开全部安全格)。"
        if r == "flag":
            return f"toggle_flag({row},{col}) 已标记为旗。"
        if r == "unflag":
            return f"toggle_flag({row},{col}) 已取消旗。"
        if r == "nochange":
            return f"{name}({row},{col}) 无变化: {out.get('message','')}"
        if r == "invalid":
            return f"{name}({row},{col}) 无效: {out.get('message','')}"
        return str(out)

    def _put(self, kind, payload):
        self.cmd_queue.put((kind, payload))


def main():
    root = tk.Tk()
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        try:
            root.tk.call("tk", "scaling", 1.25)
        except tk.TclError:
            pass
    app = App(root)
    app.thinking.tag_configure("sys", foreground="#56b6c2",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    app.thinking.tag_configure("think", foreground="#d4d4d4",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
    app.thinking.tag_configure("act", foreground="#98c379",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    app.thinking.tag_configure("res", foreground="#61afef",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE))
    app.thinking.tag_configure("err", foreground="#e06c75",
                               font=(BOARD_TEXT_FONT, THINK_FONT_SIZE, "bold"))
    root.mainloop()


if __name__ == "__main__":
    main()
