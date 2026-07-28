"""Command line entry point for the Helldivers 2 war status wallpaper.

    python run_once.py --once       run a cycle and set the wallpaper
    python run_once.py --preview    render to a temp folder and open it
    python run_once.py --dry-run    fetch and print, render nothing
    python run_once.py --restore    put the original wallpaper back
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hd2tracker import api, main, wallpaper


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_once.py",
        description="Render the Helldivers 2 galactic war status as a desktop wallpaper.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one cycle and set the wallpaper (default)")
    mode.add_argument("--preview", action="store_true", help="render to a temp folder without touching the desktop")
    mode.add_argument("--dry-run", action="store_true", help="fetch and print the resolved state only")
    mode.add_argument("--restore", action="store_true", help="restore the wallpaper captured before first run")
    parser.add_argument("--output", type=Path, help="directory for --preview output")
    parser.add_argument("--no-open", action="store_true", help="do not open preview images afterwards")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def _open_folder(path: Path) -> None:
    try:
        os.startfile(path)  # noqa: S606 - Windows-only, explicit user action
    except (OSError, AttributeError):
        subprocess.run(["explorer", str(path)], check=False)


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    main.setup_logging(args.verbose)

    if args.restore:
        return 0 if wallpaper.restore() else 1

    if args.dry_run:
        try:
            snapshot = main.build_snapshot()
        except api.ApiError as exc:
            print(f"API unavailable: {exc}", file=sys.stderr)
            return 2
        print(main.describe(snapshot))
        return 0

    if args.preview:
        output = args.output or Path(tempfile.mkdtemp(prefix="hd2_preview_"))
        output.mkdir(parents=True, exist_ok=True)
        code = main.run_cycle(preview_dir=output, apply_wallpaper=False)
        if code == 0:
            print(f"Preview written to {output}")
            for entry in sorted(output.glob("*.png")):
                print(f"  {entry.name}")
            if not args.no_open:
                _open_folder(output)
        return code

    with main.SingleInstance() as lock:
        if not lock.acquired:
            main.log.info("another instance is running; skipping this cycle")
            return 0
        return main.run_cycle()


if __name__ == "__main__":
    sys.exit(run())
