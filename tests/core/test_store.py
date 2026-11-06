"""The bounded per-repository store."""

from __future__ import annotations

import os
import time
from pathlib import Path

from house_style_core.models import Signal
from house_style_core.store import MAX_REPOS, MAX_ROWS_PER_REPO, Store, repo_key


def make_signal(index: int, path: str = "pkg/module.py") -> Signal:
    return Signal(
        id=f"s{index}",
        ts=f"2026-01-01T00:00:{index % 60:02d}+00:00",
        tool="write_file",
        path=path,
        content_sha256="0" * 64,
        hunk=[f"line {index}"],
    )


def test_construction_touches_no_disk(tmp_path: Path):
    base = tmp_path / "plugin-data" / "house-style"
    Store(base)
    assert not base.exists(), "registering a plugin must not create its data directory"


def test_append_and_read_back(tmp_path: Path):
    store = Store(tmp_path)
    store.append("k1", make_signal(1))
    store.append("k1", make_signal(2))
    rows = store.rows("k1")
    assert [row.id for row in rows] == ["s1", "s2"]
    assert rows[0].hunk == ["line 1"]


def test_repositories_do_not_share_rows(tmp_path: Path):
    store = Store(tmp_path)
    store.append(repo_key(tmp_path / "one"), make_signal(1))
    store.append(repo_key(tmp_path / "two"), make_signal(2))
    assert [row.id for row in store.rows(repo_key(tmp_path / "one"))] == ["s1"]
    assert [row.id for row in store.rows(repo_key(tmp_path / "two"))] == ["s2"]


def test_oldest_rows_are_dropped_at_the_row_cap(tmp_path: Path):
    store = Store(tmp_path)
    for index in range(MAX_ROWS_PER_REPO + 25):
        store.append("k1", make_signal(index))
    rows = store.rows("k1")
    assert len(rows) == MAX_ROWS_PER_REPO
    assert rows[-1].id == f"s{MAX_ROWS_PER_REPO + 24}"
    assert rows[0].id != "s0"


def test_meta_round_trip_and_off_flag(tmp_path: Path):
    store = Store(tmp_path)
    store.register_repo(tmp_path)
    key = repo_key(tmp_path)
    assert store.is_off(key) is False
    store.set_off(key, True)
    assert store.is_off(key) is True
    assert store.meta(key)["repo"] == str(tmp_path.resolve())


def test_last_generated_round_trip(tmp_path: Path):
    store = Store(tmp_path)
    store.set_last_generated("k1", ["Prefer pathlib (2 edits)"])
    assert store.last_generated("k1") == ["Prefer pathlib (2 edits)"]


def test_clear_removes_only_that_repository(tmp_path: Path):
    store = Store(tmp_path)
    store.append("k1", make_signal(1))
    store.append("k2", make_signal(2))
    assert store.clear("k1") == 1
    assert store.rows("k1") == []
    assert len(store.rows("k2")) == 1


def test_pending_proposal_round_trip(tmp_path: Path):
    store = Store(tmp_path)
    store.set_pending("k1", {"learned": ["a"]})
    assert store.pending("k1") == {"learned": ["a"]}
    store.clear_pending("k1")
    assert store.pending("k1") is None


def test_sweep_drops_stale_repositories(tmp_path: Path):
    store = Store(tmp_path)
    repo = tmp_path / "old"
    repo.mkdir()
    key = store.register_repo(repo)
    store.append(key, make_signal(1))
    meta = store.signals_dir / f"{key}.repo.json"
    ancient = time.time() - 200 * 86400
    os.utime(meta, (ancient, ancient))
    removed = store.sweep(stale_days=90)
    assert key in removed
    assert store.rows(key) == []


def test_sweep_keeps_recent_repositories(tmp_path: Path):
    store = Store(tmp_path)
    store.append("fresh", make_signal(1))
    store.register_repo(tmp_path / "fresh")
    assert store.sweep() == []


def test_sweep_caps_the_number_of_repositories(tmp_path: Path):
    store = Store(tmp_path)
    for index in range(MAX_REPOS + 3):
        key = f"k{index}"
        store.append(key, make_signal(index))
        store.register_repo(tmp_path / f"repo{index}")
    removed = store.sweep(max_repos=MAX_REPOS)
    assert len(removed) == 3


def test_corrupt_rows_are_skipped_not_fatal(tmp_path: Path):
    store = Store(tmp_path)
    store.append("k1", make_signal(1))
    with open(store.signals_dir / "k1.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    store.append("k1", make_signal(2))
    assert [row.id for row in store.rows("k1")] == ["s1", "s2"]
