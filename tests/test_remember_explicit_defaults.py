"""An explicitly typed `--cat`/`--imp` must reach the queue as a hint.

`remember` decides whether the caller pinned a category or an
importance so `_plan_fact` knows whether to keep the caller's value or
let the extractor's per-fact guess win. Comparing the parsed value
against the default cannot make that call: a caller who types the
default value looks identical to one who typed nothing.

`replace` already resolves this with `ctx.get_parameter_source`.
"""

import json

from tests.conftest import invoke, parse_remember


def _hints(data_dir, queue_id):
    """Return (hint_cat, hint_imp) for a queue row."""
    from memman.queue import queue_db
    with queue_db(data_dir) as conn:
        return conn.execute(
            'select hint_cat, hint_imp from queue where id = ?',
            (queue_id,)).fetchone()


def test_explicit_default_category_reaches_queue_as_hint(mm_runner):
    """Verify `--cat fact` pins the category instead of reading as unset.

    Mutation: deciding explicitness with `cat if cat != 'fact' else
        None`, which drops an explicitly typed default and hands the
        category back to the LLM extractor.
    Oracle: the queue row's `hint_cat` column, read directly.
    """
    _, data_dir = mm_runner
    result = invoke(mm_runner, [
        'remember', 'an explicitly fact categorized note',
        '--cat', 'fact'])
    queue_id = json.loads(result.output)['queue_id']

    assert _hints(data_dir, queue_id)[0] == 'fact'


def test_explicit_default_importance_reaches_queue_as_hint(mm_runner):
    """Verify `--imp 3` pins the importance instead of reading as unset.

    Mutation: deciding explicitness with `imp if imp != 3 else None`,
        which drops an explicitly typed default and hands the
        importance back to the LLM extractor.
    Oracle: the queue row's `hint_imp` column, read directly.
    """
    _, data_dir = mm_runner
    result = invoke(mm_runner, [
        'remember', 'an explicitly middling importance note',
        '--imp', '3'])
    queue_id = json.loads(result.output)['queue_id']

    assert _hints(data_dir, queue_id)[1] == 3


def test_omitted_category_stays_unset_and_importance_is_always_set(mm_runner):
    """Verify an omitted `--cat` reaches the queue NULL while `--imp` is stored.

    Mutation: keeping the ParameterSource branch for `imp`, which leaves
        `hint_imp` NULL and reopens the extractor's importance; or
        reading `cat` as explicit, which pins the default on every write
        and silences the extractor's category.
    Oracle: `(None, 3)` on the queue row when neither flag is passed.
    """
    _, data_dir = mm_runner
    result = invoke(mm_runner, [
        'remember', 'a note with no category or importance flag'])
    queue_id = json.loads(result.output)['queue_id']

    assert _hints(data_dir, queue_id) == (None, 3)


def _stub_extractor(monkeypatch, category: str, importance: int) -> None:
    """Make the extractor return one fact with the given category and importance."""
    from memman.llm import extract as llm_extract
    monkeypatch.setattr(llm_extract, 'extract_facts', lambda client, content: [
        {'text': content, 'category': category, 'importance': importance,
         'entities': []}])


def test_extractor_importance_is_ignored(mm_runner, monkeypatch):
    """Verify the extractor's importance never reaches the stored row.

    Mutation: reading the extractor's `importance` when the caller omitted
        `--imp` (the `fact.get('importance', parent.importance)` branch).
    Oracle: the stored row's importance against an extractor stub that
        returns 5: 3 with the flag omitted, 4 with `--imp 4`.
    """
    _stub_extractor(monkeypatch, 'fact', 5)
    omitted = parse_remember(
        invoke(mm_runner, ['remember', 'alpha bravo charlie delta echo']),
        mm_runner)
    pinned = parse_remember(
        invoke(mm_runner, ['remember', 'quebec romeo sierra tango uniform',
                           '--imp', '4']),
        mm_runner)

    assert (omitted['importance'], pinned['importance']) == (3, 4)


def test_omitted_cat_enqueues_no_hint_and_default_fact_reaches_the_row(
        mm_runner, monkeypatch):
    """Verify an omitted `--cat` lets the extractor decide, else stores `fact`.

    Mutation: both drain fallbacks reading `general` (the `row.hint_cat`
        default and the clamp for an unknown category), or the omitted
        flag pinning the default so the extractor's category loses.
    Oracle: `hint_cat` NULL on the queue row; the stored category is the
        stub's `decision` with the flag omitted, and `fact` under
        `--no-reconcile` with the flag omitted.
    """
    _, data_dir = mm_runner
    _stub_extractor(monkeypatch, 'decision', 3)
    extracted = invoke(mm_runner, ['remember', 'alpha bravo charlie delta echo'])
    verbatim = invoke(mm_runner, [
        'remember', 'quebec romeo sierra tango uniform', '--no-reconcile'])

    assert _hints(data_dir, json.loads(extracted.output)['queue_id'])[0] is None
    assert parse_remember(extracted, mm_runner)['category'] == 'decision'
    assert parse_remember(verbatim, mm_runner)['category'] == 'fact'
