from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
AISHE_OUTPUT = BASE_DIR / "north_india_deep_contacts.xlsx"
PIPELINE_INPUT = BASE_DIR / "north_india_deep_contacts.xlsx"
BACKFILL_SCRIPT = BASE_DIR / "backfill_placement.py"
DEEP_CRAWL_SCRIPT = BASE_DIR / "deep_crawl.py"
DATA_CLEAN_SCRIPT = BASE_DIR / "dataClean.py"
FINAL_OUTPUT_DEFAULT = BASE_DIR / "final_north_india_deep_contacts.xlsx"
DATA_CLEAN_OUTPUT = BASE_DIR / "cleaned_data.xlsx"


def run_step(script_path: Path) -> None:
    subprocess.run([sys.executable, str(script_path)], cwd=BASE_DIR, check=True)


def run_step_with_args(script_path: Path, extra_args: list[str]) -> None:
    subprocess.run([sys.executable, str(script_path), *extra_args], cwd=BASE_DIR, check=True)


def ensure_file_exists(path: Path, label: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{label} was not created: {path}")


def prepare_pipeline_input() -> None:
    ensure_file_exists(AISHE_OUTPUT, "AISHE scraper output")
    if AISHE_OUTPUT != PIPELINE_INPUT:
        shutil.copy2(AISHE_OUTPUT, PIPELINE_INPUT)


def finalize_output(final_output: Path) -> None:
    ensure_file_exists(DATA_CLEAN_OUTPUT, "Data-clean output")
    if final_output.exists():
        final_output.unlink()
    shutil.move(str(DATA_CLEAN_OUTPUT), str(final_output))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AISHE -> backfill -> deep crawl -> cleanup pipeline.")
    parser.add_argument(
        "--final-output",
        default=str(FINAL_OUTPUT_DEFAULT),
        help="Path for the final cleaned workbook.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Limit how many rows each stage processes for a test run.",
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Shortcut for --max-rows 3.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Process only this many new AISHE rows in the scraper stage.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    final_output = Path(args.final_output)
    max_rows = 3 if args.test_mode and args.max_rows <= 0 else args.max_rows
    test_stage_args = ["--max-rows", str(max_rows)] if max_rows and max_rows > 0 else []
    aishe_stage_args = list(test_stage_args)
    if args.batch_size and args.batch_size > 0:
        aishe_stage_args.extend(["--batch-size", str(args.batch_size)])

    print("[1/4] Running AISHE scraper...")
    run_step_with_args(BASE_DIR / "aishe_scraper.py", aishe_stage_args)

    print("[2/4] Preparing workbook for backfill stage...")
    prepare_pipeline_input()

    print("[3/4] Running placement backfill...")
    run_step_with_args(BACKFILL_SCRIPT, test_stage_args)

    print("[4/4] Running deep crawl and final cleanup...")
    run_step_with_args(DEEP_CRAWL_SCRIPT, test_stage_args)
    run_step_with_args(DATA_CLEAN_SCRIPT, test_stage_args)

    finalize_output(final_output)
    print(f"Pipeline complete. Final file: {final_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())