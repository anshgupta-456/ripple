"""Architecture guards: the agent side must never see ground truth or import the evaluator."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
AGENT_SIDE = ["engine", "agent", "firewall", "executor", "verifier", "baselines", "intake", "graph", "timeline"]


def test_agent_side_never_imports_bench_or_reads_ground_truth():
    pat = re.compile(r"app\.bench|ground_truth|GROUND_TRUTH")
    offenders = []
    for pkg in AGENT_SIDE:
        d = APP / pkg
        if d.exists():
            for f in d.rglob("*.py"):
                if pat.search(f.read_text(encoding="utf-8")):
                    offenders.append(str(f))
    assert not offenders, offenders
