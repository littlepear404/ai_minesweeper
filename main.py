"""AI Minesweeper entrypoint.

Run:    python main.py
Setup:  pip install -r requirements.txt
Config: llm_config.json (provider/api_base_url/api_key/model/...)
"""
import sys


def main():
    try:
        import tkinter
    except ImportError:
        print("此程序需要 Tkinter。Windows 自带;Linux 上请安装 python3-tk。", file=sys.stderr)
        sys.exit(1)
    try:
        import requests  # noqa: F401
    except ImportError:
        print("缺少依赖 requests，请先运行: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    from gui import main as gui_main
    gui_main()


if __name__ == "__main__":
    main()