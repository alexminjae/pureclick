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
    # 45, not 30: the regression this guards against measured 65.6ms, and a
    # Windows CI runner produced 33.9ms on unchanged code while a laptop sits
    # near 20ms across repeated runs. A ceiling that a noisy runner trips on
    # healthy code trains people to ignore the whole file, which costs more than
    # the 15ms of slack — and 45 still fails long before the old behaviour.
    "cartNoticeLagMs": 45.0,
    # detect -> press through pressSequence itself: bitmap flip to the pointer
    # events leaving. Measured 0.07ms median on a laptop; the target is 0.5ms.
    # 2.0 here because the bench runs on whatever CI runner picks it up, and
    # the regression this guards is a layout or a scan returning to the path,
    # which costs milliseconds, not fractions.
    "detectToPressMs": 2.0,
    # The fiber walk from a circle to the page root plus the handler call.
    "handlerPressMs": 2.0,
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
