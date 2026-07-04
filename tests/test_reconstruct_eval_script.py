"""Keyless wiring smoke for the ``scripts/reconstruct_eval.sh`` convenience path (esim-ecr.6).

The capstone harness ties the six attribution-eval steps
(build → fidelity → oracle/reconstructed/rag → report) into one command. The
oracle/reconstructed reasoners need ``ANTHROPIC_API_KEY`` (that is the crew keyed
run that fills the RECONSTRUCT.md AFTER tables), so this test exercises the
``--keyless-smoke`` mode: it forces ``--backend fake`` and substitutes one keyless
RAG prediction for all three prediction slots, proving the full plumbing
end to end — every intermediate file is produced and ``report`` renders — with no
key. It asserts the wiring, not the (stand-in) numbers.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "reconstruct_eval.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash to run the harness")
def test_keyless_smoke_produces_every_eval_artifact(tmp_path: Path) -> None:
    out = tmp_path / "eval"
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--keyless-smoke", "-o", str(out)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"harness failed:\n{result.stderr}"

    # Every stage's artifact lands under the single --out dir.
    for name in (
        "bench.jsonl",
        "fidelity.json",
        "pred.oracle.jsonl",
        "pred.reconstructed.jsonl",
        "pred.rag.jsonl",
        "attribution.md",
    ):
        assert (out / name).is_file(), f"missing {name}"
    assert (out / "recon" / "nodes.jsonl").is_file()

    # REPORT actually rendered the three-system attribution table.
    report = (out / "attribution.md").read_text(encoding="utf-8")
    assert "Reconstruct attribution report" in report
    assert "oracle" in report and "reconstructed" in report and "rag" in report

    # The smoke mode must flag itself as wiring stand-ins, not an eval result.
    assert "wiring stand-ins" in result.stderr


def test_help_lists_the_keyless_smoke_flag() -> None:
    """``--help`` renders the header doc so the harness is self-describing."""
    result = subprocess.run(
        ["bash", str(_SCRIPT), "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "--keyless-smoke" in result.stdout
    assert "attribution eval" in result.stdout
