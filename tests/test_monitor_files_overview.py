"""The Pipeline box's files overview: ordering, cluster filtering and cell flow."""

from pipeline_core.monitor.files_overview import files_overview_rows, sorted_files
from pipeline_core.monitor.render import plain


def _prog(**over):
    prog = {
        "current_file": "src/b.rs",
        "cluster_files": [],
        "files_this_pass": ["src/b.rs", "src/a.rs"],
        "all_files": ["src/old.rs"],
        "queue_history": {},
        "quick_cov_history": {},
    }
    prog.update(over)
    return prog


COV = {"src/a.rs": (5, 10), "src/b.rs": (10, 10), "src/old.rs": (1, 4),
       "tests/t.rs": (1, 1), "src/unk.rs": (1, 2)}


def test_order_is_pass_order_then_older_then_unknown_by_coverage():
    names = [n for n, *_ in sorted_files(_prog(), COV, "")]
    assert names == ["src/b.rs", "src/a.rs", "src/old.rs", "src/unk.rs"]


def test_this_pass_file_missing_from_coverage_still_appears():
    files = sorted_files(_prog(files_this_pass=["src/b.rs", "src/ghost.rs"]), COV, "")
    assert ("src/ghost.rs", 0, 0) in files


def test_cluster_filter_restricts_to_cluster_files():
    prog = _prog(cluster_files=["src/a.rs"], files_this_pass=[])
    assert [n for n, *_ in sorted_files(prog, COV, "c1")] == ["src/a.rs"]
    # the default cluster ignores the filter
    assert len(sorted_files(prog, COV, "stable")) == 4


def test_cells_show_progress_history_and_marker():
    prog = _prog(queue_history={"b.rs": [3, 7]},
                 quick_cov_history={"src/a.rs": [10.0, 20.0], "b.rs": [50.0]})
    rows = [plain(r) for r in files_overview_rows(prog, COV, "", 200)]
    assert "files overview — 1/4 complete" in rows[1]
    text = "\n".join(rows)
    assert "▶ b.rs" in text and "10/10 ✓" in text and "3→ 7" in text
    assert "10.0%→ 20.0%" in text and "50.0%" in text


def test_zero_coverable_renders_n_a():
    prog = _prog(files_this_pass=["src/ghost.rs"], all_files=[])
    assert "n/a" in "\n".join(plain(r) for r in files_overview_rows(prog, {}, "", 80))


def test_wide_box_flows_columns_and_narrow_box_sheds_the_tail():
    prog = _prog(queue_history={n.split("/")[-1]: [1, 2, 3, 4, 5] for n in COV})
    wide = files_overview_rows(prog, COV, "", 400)
    narrow = files_overview_rows(prog, COV, "", 60)
    assert len(wide) < len(narrow)
    assert all("→ " not in plain(r) for r in narrow[2:])
