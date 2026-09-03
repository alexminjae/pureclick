"""The catch path has a latency budget, and it is asserted, not eyeballed.

취켓팅 loses to 이선좌 in the gap between a seat freeing and the hold landing on
the page, and every regression in that gap is silent: nothing breaks, nothing
errors, the macro simply comes second. These are the segments that were
actually measured slow, with the numbers they were measured at:

    currentOpenBlock()   925ms  — a nested scan of every seat in the house, run
                                  per drawn seat, on the travel decision made
                                  the instant a seat frees
    clickSeatOnMap()     2.7ms  — a linear walk of the 구역 with a fiber read
                                  per circle, to find one seat we already had
                                  an index for
    checkDomAgreement()  2.5ms  — the same walk again, once per watched seat,
                                  on every tick, and again inside the mutation
                                  callback
    cart notice lag     65.6ms  — the settle ramp jumped from 16ms to 80ms after
                                  six tries, so a soft hold that landed at 280ms
                                  sat unnoticed inside an 80ms gap

The ceilings are deliberately loose — a CI runner is slower and noisier than a
laptop, and this is here to catch an algorithm going back to O(n), not to
police a few hundred microseconds.

Run the bench by hand for the readable table:  node tests/bench_catch_latency.mjs
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "tests" / "bench_catch_latency.mjs"

# median ms, on a synthetic 21,600-seat venue with an 1,800-seat 구역 mounted.
CEILINGS = {
    "currentOpenBlockMs": 100.0,
    "clickSeatOnMapMs": 25.0,
    "checkDomAgreementMs": 25.0,
    "cartNoticeLagMs": 30.0,
}


@unittest.skipUnless(shutil.which("node"), "node is needed to run the autopilot bench")
class TheCatchPathStaysFast(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run(
            ["node", str(BENCH), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if done.returncode != 0:
            raise AssertionError(f"bench failed:\n{done.stdout}\n{done.stderr}")
        cls.measured = json.loads(done.stdout.strip().splitlines()[-1])

    def test_no_segment_of_the_hot_path_went_back_to_walking_the_venue(self) -> None:
        for name, ceiling in CEILINGS.items():
            with self.subTest(segment=name):
                got = self.measured[name]
                self.assertLess(
                    got, ceiling,
                    f"{name} is {got:.1f}ms, over its {ceiling:.0f}ms ceiling — "
                    f"something on the catch path is scanning again",
                )

    def test_the_quiet_gap_before_confirm_is_still_held(self) -> None:
        """The one wait that must NOT be optimised away.

        NOL's own seat bundle refuses 선택 완료 while its preselect is in flight
        and answers seat_requestPending; pressing early is the P40021 /
        좌석 선택 도중 오류 failure, which costs the seat outright. What was
        shortened is when the gap starts counting — from the page's own answer
        rather than from when we noticed it — not the gap itself.
        """
        self.assertGreater(
            self.measured["quietGapMs"], 100.0,
            "the soft-hold quiet gap has been shortened past what protects the confirm",
        )


if __name__ == "__main__":
    unittest.main()
