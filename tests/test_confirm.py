import pytest

from slurm_mcp.confirm import ConfirmationStore
from slurm_mcp.errors import SlurmMcpError


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_token_is_single_use():
    store = ConfirmationStore(clock=Clock())
    tok = store.issue("submit", ["sbatch", "x"], "sbatch x")
    assert store.redeem(tok, "submit").remote_args == ("sbatch", "x")
    with pytest.raises(SlurmMcpError, match="unknown or expired"):
        store.redeem(tok, "submit")


def test_token_expires():
    clock = Clock()
    store = ConfirmationStore(ttl_s=60, clock=clock)
    tok = store.issue("submit", ["sbatch", "x"], "s")
    clock.now = 61
    with pytest.raises(SlurmMcpError, match="expired"):
        store.redeem(tok, "submit")


def test_token_cannot_be_used_for_another_action():
    store = ConfirmationStore(clock=Clock())
    tok = store.issue("submit", ["sbatch", "x"], "s")
    with pytest.raises(SlurmMcpError, match="not cancel"):
        store.redeem(tok, "cancel")
    # a wrong-kind attempt does not burn the token
    assert store.redeem(tok, "submit")


def test_unknown_token_and_bounded_store():
    store = ConfirmationStore(clock=Clock(), max_pending=2)
    with pytest.raises(SlurmMcpError):
        store.redeem("nope", "submit")
    first = store.issue("submit", ["a"], "a")
    store.issue("submit", ["b"], "b")
    store.issue("submit", ["c"], "c")
    with pytest.raises(SlurmMcpError):
        store.redeem(first, "submit")  # oldest was dropped
