import json
from pathlib import Path
from checkpoint_test import LocalStateStore, new_state, apply_result, run_simulation


def test_seed_resumes_at_next_number():
    s = new_state(1658164)
    assert s['next_candidate'] == 1658165
    assert s['highest_confirmed_id'] == 1658164


def test_five_misses_stop_but_do_not_skip_late_record():
    s = new_state(1658164)
    for i in range(1658165, 1658169):
        s, stop = apply_result(s, i, 'NOT_FOUND', 5)
        assert not stop
    s, stop = apply_result(s, 1658169, 'NOT_FOUND', 5)
    assert stop
    assert s['next_candidate'] == 1658165


def test_found_resets_streak_and_moves_cursor():
    s = new_state(1658164)
    s, _ = apply_result(s, 1658165, 'NOT_FOUND', 5)
    s, stop = apply_result(s, 1658166, 'FOUND', 5)
    assert not stop
    assert s['consecutive_missing'] == 0
    assert s['next_candidate'] == 1658167
    assert s['highest_confirmed_id'] == 1658166


def test_errors_not_counted_as_missing():
    s = new_state(1658164)
    s, stop = apply_result(s, 1658165, 'ERROR', 5)
    assert stop
    assert s['consecutive_missing'] == 0
    assert s['next_candidate'] == 1658165


def test_two_runs_continue_from_persistent_cloud_storage_shape(tmp_path):
    store = LocalStateStore(str(tmp_path / 'checkpoint.json'))
    a = run_simulation(store)
    assert a['run_count'] == 1
    assert a['highest_confirmed_id'] == 1658167
    assert a['next_candidate'] == 1658168
    b = run_simulation(store)
    assert b['run_count'] == 2
    assert b['highest_confirmed_id'] == 1658170
    assert b['next_candidate'] == 1658171
    saved = json.loads((tmp_path / 'checkpoint.json').read_text())
    assert saved == b
    assert saved['test_mode'] == 'OFFLINE_SIMULATION_ONLY'


def test_does_not_create_fake_archive_file(tmp_path):
    store = LocalStateStore(str(tmp_path / 'checkpoint-demo.json'))
    run_simulation(store)
    assert not (tmp_path / 'archive.json').exists()
