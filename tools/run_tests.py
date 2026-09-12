"""Run the offline suite and enforce separate line and branch coverage gates."""

from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "coverage-report"
MINIMUM = 91


def run(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "coverage", *args], cwd=ROOT, check=True)


def main() -> int:
    REPORT.mkdir(exist_ok=True)
    run("run", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v")
    run("report", "-m")
    run("json", "-o", str(REPORT / "coverage.json"))
    run("xml", "-o", str(REPORT / "coverage.xml"))
    run("html", "-d", str(REPORT / "html"))
    totals = json.loads((REPORT / "coverage.json").read_text())["totals"]
    failed = False
    for label, covered, total in (
        ("Lines", totals["covered_lines"], totals["num_statements"]),
        ("Branches", totals["covered_branches"], totals["num_branches"]),
    ):
        percentage = 100 * covered / total if total else 100
        print(
            f"{label}: {percentage:.2f}% ({covered}/{total}); required: {MINIMUM}%",
            flush=True,
        )
        failed |= percentage < MINIMUM
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
