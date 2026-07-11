# tests/test_guardrails.py
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _ignored(path: str) -> bool:
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    return r.returncode == 0


def test_markdown_never_tracked():
    assert _ignored("CONTEXT.md")
    assert _ignored("anything/nested/note.md")


def test_docs_dir_never_tracked():
    assert _ignored("docs/adr/0001-hermes-preconfigured-distribution.md")
    assert _ignored("docs/superpowers/plans/x.md")


def test_secrets_never_tracked():
    assert _ignored("secrets/channel.key")
    assert _ignored(".env")


def test_no_md_currently_staged():
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=ROOT, capture_output=True, text=True,
    )
    staged = [l for l in r.stdout.splitlines() if l.strip()]
    assert not [f for f in staged if f.endswith(".md") or f.startswith("docs/")]
