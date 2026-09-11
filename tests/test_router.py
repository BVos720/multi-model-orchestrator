import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.router import route_step  # noqa: E402


def test_declared_complexity_wins():
    assert route_step("literally anything", declared_complexity="high").tier == "cloud"
    assert route_step("literally anything", declared_complexity="low").tier == "local"


def test_complex_keyword_routes_cloud():
    decision = route_step("Design the overall architecture for the auth module")
    assert decision.tier == "cloud"


def test_simple_keyword_routes_local():
    decision = route_step("Add a trivial boilerplate getter for the User class")
    assert decision.tier == "local"


def test_short_step_defaults_local():
    decision = route_step("Rename `usr` to `user` in utils.py")
    assert decision.tier == "local"


def test_ambiguous_long_step_defaults_cloud():
    long_desc = " ".join(["word"] * 40)
    decision = route_step(long_desc)
    assert decision.tier == "cloud"


def test_bias_zero_forces_cloud_even_for_declared_low():
    # deliberately no simple/complex keyword hit and >8 words, so only the
    # declared "low" complexity would normally route this local - bias=0
    # should override that and escalate anyway.
    desc = "Add a getter method for the internal cache field used by the session manager"
    decision = route_step(desc, declared_complexity="low", local_bias=0)
    assert decision.tier == "cloud"


def test_bias_ten_tries_local_even_for_declared_high():
    decision = route_step("Refactor the whole module", declared_complexity="high", local_bias=10)
    assert decision.tier == "local"


def test_bias_ten_still_escalates_complex_keyword():
    decision = route_step("Design the overall architecture", local_bias=10)
    assert decision.tier == "cloud"


def test_security_step_escalates_regardless_of_bias():
    decision = route_step("Check for security issues in this tiny snippet", local_bias=9)
    assert decision.tier == "cloud"


def test_higher_bias_widens_the_simple_word_threshold():
    medium_desc = " ".join(["word"] * 30)  # over the default threshold (25), under a high-bias one
    assert route_step(medium_desc, local_bias=5).tier == "cloud"
    assert route_step(medium_desc, local_bias=8).tier == "local"


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok  - {t.__name__}")
        except AssertionError:
            failures += 1
            print(f"FAIL - {t.__name__}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
