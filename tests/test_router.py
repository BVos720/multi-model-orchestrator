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
