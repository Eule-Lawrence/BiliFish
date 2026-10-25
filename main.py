"""程序入口。

    python main.py            打开界面（默认）
    python main.py --login    命令行扫码登录，Cookie 自动写进配置
    python main.py --cli      无界面跑一轮（看日志用）
    python main.py --cli --loop   无界面常驻
    python main.py --selftest     自检（打包后排查问题用）
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from config import APP_TITLE, APP_VERSION, Config


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="BiliFish", description=APP_TITLE)
    p.add_argument("--version", action="store_true", help="显示版本号")
    p.add_argument("--cli", action="store_true", help="不打开界面，直接在命令行运行")
    p.add_argument("--loop", action="store_true", help="命令行模式下常驻循环")
    p.add_argument("--dry-run", action="store_true", help="演练：只生成不发送")
    p.add_argument("--selftest", action="store_true", help="环境自检")
    p.add_argument("--login", action="store_true", help="命令行扫码登录")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.version:
        print(f"{APP_TITLE} v{APP_VERSION}")
        return 0

    if args.selftest:
        return selftest()

    if args.login:
        from login import login_cli

        _force_utf8()
        return asyncio.run(login_cli())

    # 单实例：已经有一个在跑，就只把它的窗口叫出来，自己退出
    from platform_utils import ensure_single_instance

    if not ensure_single_instance():
        if args.cli:
            print("已经有一个大肥鱼在跑了，这次不重复启动。")
        return 0

    if args.cli:
        return run_cli(args)

    # 默认：打开图形界面
    try:
        from gui import run

        run()
        return 0
    except Exception as exc:  # 界面起不来时给个能看懂的提示
        if getattr(sys, "frozen", False):
            _show_error(f"界面启动失败：{exc}")
        raise


# ---------------------------------------------------------------------------
# 命令行模式
# ---------------------------------------------------------------------------

def run_cli(args: argparse.Namespace) -> int:
    import logutil
    from runner import Runner
    from storage import StateStore

    _force_utf8()
    logutil.setup(console=True)

    cfg = Config.load()
    if args.dry_run:
        cfg.dry_run = True
    if args.loop:
        cfg.loop_enabled = True

    errors = cfg.validate()
    if errors:
        print("配置不完整：")
        for e in errors:
            print("  -", e)
        print("\n先运行 `python main.py login` 扫码登录，或直接打开界面 `python main.py`。")
        return 2

    print(f"{APP_TITLE} v{APP_VERSION}")
    print(f"配置：{cfg.masked()}")
    print(f"模式：{'演练' if cfg.dry_run else '正式'} | 每日上限 {cfg.daily_limit}\n")

    store = StateStore().load()
    runner = Runner(cfg, store)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("\n已手动中断")
    return 0


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def selftest() -> int:
    import platform

    lines: list[str] = [f"{APP_TITLE} v{APP_VERSION} 自检", f"时间：{__import__('time').strftime('%Y-%m-%d %H:%M:%S')}", ""]
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            lines.append(f"[ OK ] {name}：{fn()}")
        except Exception as exc:
            ok = False
            lines.append(f"[FAIL] {name}：{type(exc).__name__}: {exc}")

    check("Python", lambda: platform.python_version())
    check("系统", lambda: f"{platform.system()} {platform.release()}")

    def check_biliapi():
        import bilibili_api

        return bilibili_api.BILIBILI_API_VERSION

    def check_data():
        from bilibili_api.utils.utils import get_api

        api = get_api("homepage")
        assert "info" in api and "videos" in api["info"]
        return "内置 JSON 接口表加载正常"

    def check_client():
        from bilibili_api.clients.HTTPXClient import HTTPXClient

        return HTTPXClient.__name__

    check("bilibili-api", check_biliapi)
    check("bilibili-api 数据文件", check_data)
    check("HTTP 客户端", check_client)
    check("httpx", lambda: __import__("httpx").__version__)
    check("openai", lambda: __import__("openai").__version__)
    def check_qrcode():
        import qrcode
        from qrcode.constants import ERROR_CORRECT_H  # noqa: F401  确认子模块也打进去了

        return getattr(qrcode, "__version__", "已加载（打包后无版本元数据）")

    check("Pillow", lambda: __import__("PIL").__version__)
    check("qrcode", check_qrcode)
    check("tkinter", lambda: __import__("tkinter").TkVersion)

    try:
        from config import app_home

        lines.append(f"[ OK ] 数据目录：{app_home()}")
    except Exception as exc:
        ok = False
        lines.append(f"[FAIL] 数据目录：{exc}")

    lines.append("")
    lines.append("结论：" + ("全部正常" if ok else "存在问题，请把本文件发给开发者"))

    text = "\n".join(lines)
    try:
        from config import logs_dir

        out = logs_dir() / "selftest.log"
        out.write_text(text, encoding="utf-8")
        text += f"\n\n已保存到：{out}"
    except Exception:
        pass

    print(text)
    if getattr(sys, "frozen", False) and not __import__("os").environ.get("BILIFISH_NO_DIALOG"):
        _show_error(text, title="自检结果")
    return 0 if ok else 1


# ---------------------------------------------------------------------------

def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文和二维码会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _show_error(text: str, title: str = "出错了") -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, text[:2000])
        root.destroy()
    except Exception:
        logging.getLogger(__name__).error(text)


if __name__ == "__main__":
    sys.exit(main())
