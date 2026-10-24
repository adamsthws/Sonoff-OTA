#!/usr/bin/env python3
"""The unit tests, side by side: what `python3 -m unittest discover -s tests`
runs, in a fraction of the time.

    python3 tests/run.py               all of them
    python3 tests/run.py -j 4          on 4 processes (default: 2 per core)
    python3 tests/run.py test_output test_detect.EsphomeLoginTests
                                       those modules, classes or tests
    python3 tests/run.py --shuffle     in a random order, to find a test that
                                       passes only after another (--seed N
                                       repeats one)

Most of the tests' time is waiting - on timeouts, sockets, terminals - not
computing, so each test runs on its own in a pool of more processes than
there are cores, the longest first - as long as each took the last time, kept
in .test-times.json (Git ignores it). What a passing test prints is not
shown, as with unittest's --buffer; a failing one's is, with its traceback.
The workflows still run `python -m unittest discover -s tests`, under coverage.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import multiprocessing
import os
import random
import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
TIMES = ROOT / ".test-times.json"
# Classes whose tests share something of the whole computer's, so must not
# run at the same time as each other: each is run as one, on one process.
TOGETHER = {
    "test_awake.AwakeTests": "systemd's sleep inhibitors: one test takes a hold while another checks none is left",
}


def setup_path() -> None:
    # As `python -m unittest discover -s tests` from the root: both importable.
    for path in (str(ROOT), str(TESTS)):
        if path not in sys.path:
            sys.path.insert(0, path)


def flatten(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from flatten(test)
        else:
            yield test


def collect(names: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Each test's id, and each module that would not even load, with why."""
    loader = unittest.TestLoader()
    suite = (unittest.TestSuite(loader.loadTestsFromName(name) for name in names) if names
             else loader.discover(str(TESTS), top_level_dir=str(TESTS)))
    ids, broken = [], []
    for test in flatten(suite):
        # A module that does not import comes back as a test that fails saying why.
        if type(test).__name__ == "_FailedTest":
            broken.append((test._testMethodName, str(getattr(test, "_exception", ""))))
        else:
            ids.append(test.id())
    return ids, broken


def run_one(test_id: str) -> tuple[str, int, list[tuple[str, str]], int, float]:
    """(id, tests run, [(kind, report)], skipped, seconds), from a worker process."""
    os.chdir(ROOT)
    started = time.monotonic()
    out = io.StringIO()
    # What a test prints is in its report if it fails; never on the screen as well.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = unittest.TextTestRunner(stream=out, verbosity=0, buffer=True).run(
            unittest.defaultTestLoader.loadTestsFromName(test_id))
    problems = [("FAIL", text) for _, text in result.failures] + \
               [("ERROR", text) for _, text in result.errors] + \
               [("UNEXPECTED SUCCESS", "") for _ in result.unexpectedSuccesses]
    return test_id, result.testsRun, problems, len(result.skipped), time.monotonic() - started


def load_times() -> dict[str, float]:
    try:
        return {str(k): float(v) for k, v in json.loads(TIMES.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-j", "--jobs", type=int, default=2 * (os.cpu_count() or 2))
    parser.add_argument("--shuffle", action="store_true", help="in a random order")
    parser.add_argument("--seed", type=int, help="that random order again")
    parser.add_argument("names", nargs="*", help="modules, classes or tests, as unittest names them")
    args = parser.parse_args(argv)
    setup_path()
    os.chdir(ROOT)
    started = time.monotonic()
    ids, broken = collect(args.names)
    together = [name for name in TOGETHER if any(test_id.startswith(name + ".") for test_id in ids)]
    tasks = together + [test_id for test_id in ids if test_id.rsplit(".", 1)[0] not in TOGETHER]
    # The longest first, so none is left to start at the end; one not timed
    # yet as if long, and a class kept together is one long task.
    times = load_times()
    tasks.sort(key=lambda task: -times.get(task, 60.0 if task in together else 1.0))
    if args.shuffle or args.seed is not None:
        seed = args.seed if args.seed is not None else random.randrange(1 << 32)
        random.Random(seed).shuffle(tasks)
        print(f"In a random order: --seed {seed} runs it again.")
    ran = skipped = 0
    problems: list[tuple[str, str, str]] = [("ERROR", f"{name} (would not load)", why) for name, why in broken]
    with multiprocessing.Pool(args.jobs, initializer=setup_path) as pool:
        for test_id, count, found, skips, seconds in pool.imap_unordered(run_one, tasks):
            times[test_id] = round(seconds, 3)
            ran += count
            skipped += skips
            problems += [(kind, test_id, text) for kind, text in found]
            print("." if not found else "F", end="", flush=True)
    print()
    try:
        TIMES.write_text(json.dumps(times, indent=0, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # only an order for next time
    for kind, test_id, text in sorted(problems, key=lambda p: p[1]):
        print("=" * 70, f"{kind}: {test_id}", "-" * 70, text, sep="\n")
    print("-" * 70)
    print(f"Ran {ran} tests in {time.monotonic() - started:.3f}s on {args.jobs} processes\n")
    failures = sum(kind == "FAIL" for kind, _, _ in problems)
    errors = len(problems) - failures
    if problems:
        print(f"FAILED (failures={failures}, errors={errors}" + (f", skipped={skipped})" if skipped else ")"))
        return 1
    print("OK" + (f" (skipped={skipped})" if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
