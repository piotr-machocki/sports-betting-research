"""Which Betclic collector a run uses when it names none (scripts/run_once.py)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_once  # noqa: E402


@pytest.mark.parametrize(
    ("present", "expected"),
    [
        ({"feed", "scroll"}, "feed"),
        ({"scroll"}, "tournaments"),
        ({"feed"}, "search"),  # the feed reuses betclic_scroll.py
        (set(), "search"),
    ],
)
def test_default_mode_follows_the_files_present(tmp_path, monkeypatch, present, expected):
    for name, constant in (("feed", "BETCLIC_FEED_SCRIPT"), ("scroll", "BETCLIC_SCROLL_SCRIPT")):
        path = tmp_path / f"{name}.py"
        if name in present:
            path.touch()
        monkeypatch.setattr(run_once, constant, path)

    assert run_once.default_betclic_mode() == expected


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([], False),
        (["--no-monitor"], False),  # the old flag still parses
        (["--monitor"], True),
    ],
)
def test_monitor_terminal_opens_only_on_request(monkeypatch, flags, expected):
    monkeypatch.setattr(sys, "argv", ["run_once.py", *flags])

    assert run_once.parse_args().monitor is expected


# ------------------------------------------- without Pinnacle (20 Nov 2026) ---


@pytest.fixture
def book_steps(monkeypatch, tmp_path):
    """Records the book collectors run; `results["Betclic"] = False` makes one fail."""
    calls, results = [], {}
    superbet = tmp_path / "superbet.py"
    superbet.touch()
    monkeypatch.setattr(run_once, "SUPERBET_SCRIPT", superbet)
    monkeypatch.setattr(run_once.run_ledger, "LEDGER_FILE", tmp_path / "runs.jsonl")
    monkeypatch.setattr(run_once.run_ledger, "MARKETS_DIR", tmp_path / "markets")
    monkeypatch.setattr(run_once.match_results, "run_step", lambda: calls.append("results"))

    def step(name, command):
        calls.append((name, command[2:]))
        return results.get(name, True)

    monkeypatch.setattr(run_once, "run_book_step", step)
    monkeypatch.setattr(run_once, "collect_markets", lambda run_id: calls.append(("markets", run_id)))
    return calls, results


def books_args(*flags):
    import argparse

    return argparse.Namespace(
        no_superbet="--no-superbet" in flags, no_results="--no-results" in flags,
        no_markets="--no-markets" in flags,
    )


def test_without_pinnacle_both_books_save_their_whole_offer(book_steps):
    calls, _ = book_steps

    code = run_once.books_only_run(books_args(), "feed", failed=False)

    (superbet, sb_args), (betclic, bc_args), (markets, markets_id), last = calls
    assert (superbet, betclic, markets, last) == ("Superbet", "Betclic", "markets", "results")
    assert markets_id == sb_args[1]
    assert sb_args[0] == "--session-id" and sb_args[1].startswith("books_")
    assert sb_args[2:] == ["--no-input"]
    assert bc_args == [sb_args[0], sb_args[1], "--all-competitions"]
    assert code == 0


@pytest.mark.parametrize("failing", ["Superbet", "Betclic"])
def test_one_book_failing_does_not_stop_the_other(book_steps, failing):
    calls, results = book_steps
    results[failing] = False

    code = run_once.books_only_run(books_args(), "feed", failed=False)

    assert [call[0] for call in calls[:2]] == ["Superbet", "Betclic"]
    assert code == 0


def test_no_book_saved_or_pinnacle_failed_exits_1(book_steps):
    _, results = book_steps
    assert run_once.books_only_run(books_args(), "feed", failed=True) == 1

    results.update(Superbet=False, Betclic=False)
    assert run_once.books_only_run(books_args(), "feed", failed=False) == 1


def test_books_only_runs_are_recorded_in_the_run_ledger(book_steps):
    _, results = book_steps
    results["Betclic"] = False
    pinnacle = run_once.run_ledger.state("failed", "exit: 403")

    run_once.books_only_run(
        books_args("--no-markets"), "feed", failed=True,
        started_at="2026-11-20T10:00:00Z", pinnacle_state=pinnacle,
    )

    [run] = run_once.run_ledger.read()
    assert run["kind"] == "books" and run["run_id"].startswith("books_")
    assert run["started_at"] == "2026-11-20T10:00:00Z" and run["status"] == "failed"
    sources = {name: each["state"] for name, each in run["sources"].items()}
    assert sources == {"pinnacle": "failed", "betclic": "failed", "superbet": "ok",
                       "polymarket": "skipped", "kalshi": "skipped"}
    assert run["sources"]["pinnacle"]["detail"] == "exit: 403"


def test_browser_modes_skip_betclic_without_a_pinnacle_list(book_steps):
    calls, _ = book_steps

    run_once.books_only_run(books_args("--no-results"), "tournaments", failed=False)

    assert [call[0] for call in calls] == ["Superbet", "markets"]


def test_markets_can_be_skipped(book_steps):
    calls, _ = book_steps

    run_once.books_only_run(books_args("--no-markets", "--no-results"), "feed", failed=False)

    assert [call[0] for call in calls] == ["Superbet", "Betclic"]


@pytest.fixture
def isolated_run(monkeypatch, tmp_path):
    """main() up to Pinnacle, with every folder in tmp_path; books_only_run recorded."""
    for name in ("PINNACLE_RAW_DIR", "BETCLIC_RAW_DIR", "INCOMING_DIR", "SESSIONS_DIR"):
        monkeypatch.setattr(run_once, name, tmp_path / name)
    monkeypatch.setattr(run_once, "STOP_FILE", tmp_path / "STOP")
    monkeypatch.setattr(sys, "argv", ["run_once.py", "--feed"])
    seen = []
    monkeypatch.setattr(
        run_once, "books_only_run",
        lambda args, mode, failed, **kwargs: seen.append((mode, failed)) or (1 if failed else 0),
    )
    return seen


def test_a_failed_pinnacle_request_still_collects_the_books(monkeypatch, isolated_run):
    def suspended():
        raise RuntimeError("403 Client Error: Forbidden")

    monkeypatch.setattr(run_once.pinnacle, "api_key", lambda: "key")
    monkeypatch.setattr(run_once.pinnacle, "main", suspended)

    with pytest.raises(SystemExit) as exc:
        run_once.main()

    assert isolated_run == [("feed", True)]
    assert exc.value.code == 1


def test_a_pinnacle_exit_such_as_429_still_collects_the_books(monkeypatch, isolated_run):
    monkeypatch.setattr(run_once.pinnacle, "api_key", lambda: "key")
    monkeypatch.setattr(run_once.pinnacle, "main", lambda: sys.exit("429: rate limited."))

    with pytest.raises(SystemExit) as exc:
        run_once.main()

    assert isolated_run == [("feed", True)]
    assert exc.value.code == 1


def test_no_pinnacle_key_is_a_books_only_run_that_succeeds(monkeypatch, isolated_run):
    monkeypatch.setattr(run_once.pinnacle, "api_key", lambda: None)

    with pytest.raises(SystemExit) as exc:
        run_once.main()

    assert isolated_run == [("feed", False)]
    assert exc.value.code == 0


def test_markets_run_in_the_background_and_a_failure_only_warns(monkeypatch, tmp_path, capsys):
    script = tmp_path / "markets.py"
    monkeypatch.setattr(run_once, "MARKETS_SCRIPT", script)

    script.write_text("import sys; sys.exit(0)")
    process = run_once.start_markets("session_x")
    assert process is not None and run_once.finish_markets(process)

    script.write_text("import sys; sys.exit(1)")
    assert not run_once.finish_markets(run_once.start_markets("session_x"))
    assert "prediction markets failed: exit code 1" in capsys.readouterr().out
    assert not run_once.finish_markets(None)
