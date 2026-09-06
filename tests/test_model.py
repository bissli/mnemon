"""Tests for memman.model -- Insight/Edge dataclasses and helpers."""

from datetime import datetime, timezone

from memman.store.model import VALID_CATEGORIES, VALID_EDGE_TYPES, Edge
from memman.store.model import Insight, format_float, format_timestamp
from memman.store.model import insight_to_brief_dict, parse_timestamp


def test_parse_entities_null():
    """JSON 'null' produces empty list, not None.

    Real branch: `parse_entities` has an explicit `if entities is None`
    fallback after json.loads. Removing that line leaks a `None`-typed
    `.entities` to downstream `.append`/iteration which crashes.
    """
    ins = Insight()
    ins.parse_entities('null')
    assert ins.entities == []


def test_valid_categories():
    """The five categories are accepted; `general` and unknowns are not.

    Mutation: the `general` literal returning to `VALID_CATEGORIES`.
    Oracle: the five-member set, with `general` and `bogus` outside it.
    """
    assert VALID_CATEGORIES == {'preference', 'decision', 'fact',
                                'insight', 'context'}
    assert 'general' not in VALID_CATEGORIES
    assert 'bogus' not in VALID_CATEGORIES


def test_parse_metadata_null():
    """JSON 'null' produces empty dict, not None.

    Real branch: parse_metadata has an explicit None fallback after
    json.loads.
    """
    e = Edge()
    e.parse_metadata('null')
    assert e.metadata == {}


def test_parse_metadata_invalid_json():
    """Invalid JSON produces empty dict via try/except fallback.

    Real branch: removing the try/except would propagate JSONDecodeError
    through every Edge read with corrupted JSON.
    """
    e = Edge()
    e.parse_metadata('not json')
    assert e.metadata == {}


def test_valid_edge_types():
    """All 4 edge types accepted, invalid rejected."""
    for et in ('temporal', 'semantic', 'causal', 'entity'):
        assert et in VALID_EDGE_TYPES
    assert 'narrative' not in VALID_EDGE_TYPES


def test_semantic_default_values():
    """Pin semantically-meaningful dataclass defaults.

    These four are real downstream-consumer contracts: changing any
    of them silently shifts graph behavior or LLM-output fallbacks.

    Mutation: the `general` literal returning as the `Insight` default.
    Oracle: the dataclass defaults.
    """
    ins = Insight()
    assert ins.category == 'fact'
    assert ins.importance == 3
    e = Edge()
    assert e.edge_type == 'semantic'
    assert e.weight == 0.5


def test_format_timestamp():
    """Verify Z-suffix timestamp format."""
    dt = datetime(2024, 1, 15, 14, 30, 45, tzinfo=timezone.utc)
    assert format_timestamp(dt) == '2024-01-15T14:30:45Z'


def test_parse_timestamp_z():
    """Parse Z-suffix timestamp."""
    dt = parse_timestamp('2024-01-15T14:30:45Z')
    assert dt.year == 2024
    assert dt.hour == 14


def test_parse_timestamp_offset():
    """Parse +00:00 suffix timestamp."""
    dt = parse_timestamp('2024-01-15T14:30:45+00:00')
    assert dt.year == 2024


def test_format_float():
    """Verify 4 decimal place formatting."""
    assert format_float(0.85) == '0.8500'
    assert format_float(1.0) == '1.0000'


BRIEF_TS = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
BRIEF_TS_OTHER = datetime(2027, 8, 9, 10, 11, 12, tzinfo=timezone.utc)


def test_brief_dict_prefers_populated_summary():
    """A populated summary is emitted verbatim, with no truncation marker.

    Mutation: always taking the content-prefix fallback, ignoring a
        populated summary.
    Oracle: hand-built insight whose summary is a distinct short string
        from its 400-char content.
    """
    ins = Insight(id='b1', created_at=BRIEF_TS, content='x' * 400,
                  category='fact',
                  importance=4, summary='the short summary')
    out = insight_to_brief_dict(ins)
    assert out['summary'] == 'the short summary'
    assert 'truncated' not in out


def test_brief_dict_falls_back_to_cut_content():
    """An empty summary falls back to a cut content prefix, marked truncated.

    Mutation: a naive summary-only projection that emits '' when the
        enrichment compression gate blanked the summary -- 46 of 118
        rows on a live store, so a third of results come back empty.
    Oracle: hand-computed 200-char prefix of a 400-char content.
    """
    ins = Insight(id='b2', created_at=BRIEF_TS, content='abcde' * 80,
                  category='fact',
                  importance=3, summary='')
    out = insight_to_brief_dict(ins)
    assert out['summary'] == 'abcde' * 40
    assert out['truncated'] is True


def test_brief_dict_projects_exact_fields_and_values():
    """The projection is exactly five keys carrying the right five values.

    Mutation: reading an adjacent field -- `ins.source` for `category`,
        `ins.access_count` for `importance`, or `ins.updated_at`
        for `created_at`. All sit beside the real ones in
        `insight_to_delta_dict` and `insight_to_full_dict`, which this
        projection was written from, so a copy slip lands on them. A
        key-set assertion cannot see it.
    Oracle: the whole expected dict, hand-written, with `created_at`
        formatted the way the full projection formats it.
    """
    ins = Insight(id='b3', created_at=BRIEF_TS,
                  updated_at=BRIEF_TS_OTHER, content='c' * 400,
                  category='decision',
                  importance=5, entities=['alpha'], source='agent',
                  access_count=7, summary='s')
    assert insight_to_brief_dict(ins) == {
        'id': 'b3',
        'category': 'decision',
        'importance': 5,
        'created_at': '2026-03-04T05:06:07Z',
        'summary': 's',
        }


def test_brief_dict_never_cuts_a_real_summary():
    """A summary longer than the limit is emitted whole, not sliced.

    Mutation: applying `[:BRIEF_CONTENT_CHARS]` to both branches -- a
        plausible tidy-up that caps every text field uniformly. It
        would silently drop the tail of a long summary and set no
        marker, since the marker keys off content length.
    Oracle: a 300-char summary, hand-sized above the 200-char limit.
    """
    long_summary = 'w' * 300
    ins = Insight(id='b6', created_at=BRIEF_TS, content='c' * 400,
                  category='fact',
                  importance=3, summary=long_summary)
    out = insight_to_brief_dict(ins)
    assert out['summary'] == long_summary
    assert 'truncated' not in out


def test_brief_dict_does_not_mark_uncut_content():
    """Content that fits under the limit is emitted whole and unmarked.

    Mutation: marking every summary-less row `truncated`. The
        enrichment compression gate blanks summaries precisely when
        content is short, so 230 of the 253 summary-less rows across
        ten live stores are under the limit -- the marker would be
        false on 91% of the rows it fires on, and each one sends the
        caller to `insights show` for a row it already holds whole.
    Oracle: a 32-char content, well under `BRIEF_CONTENT_CHARS`.
    """
    ins = Insight(id='b4', created_at=BRIEF_TS,
                  content='Use `make test` to run the suite',
                  category='fact', importance=3, summary='')
    out = insight_to_brief_dict(ins)
    assert out['summary'] == 'Use `make test` to run the suite'
    assert 'truncated' not in out


def test_brief_dict_treats_blank_summary_as_absent():
    """A whitespace-only summary takes the content fallback, not verbatim.

    Mutation: a bare `if ins.summary:` truthiness test, which passes
        '   ' straight through and returns a row with nothing to read.
    Oracle: a 400-char content whose hand-computed 200-char prefix must
        appear instead of the spaces.
    """
    ins = Insight(id='b5', created_at=BRIEF_TS, content='abcde' * 80,
                  category='fact',
                  importance=3, summary='   ')
    out = insight_to_brief_dict(ins)
    assert out['summary'] == 'abcde' * 40
    assert out['truncated'] is True
