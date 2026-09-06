"""Tests for memman.llm.shared -- reading the JSON object out of a response.

The reconciler and the extractor both read their verdict through
`parse_json_response`. A response the model wraps in reasoning, corrects
after a first attempt, or mis-escapes must still yield its final object,
or the write path lands the fact as ADD and the verdict is lost.
"""

import json

from memman.llm.shared import parse_json_list_response, parse_json_response


def test_parse_json_response_reads_the_object_after_prose():
    """Verify a paragraph of reasoning before the JSON does not hide it.

    Mutation: trying only the whole text and the whole text with its
        fences stripped (the pre-0.34.0 reader), which returns None here.
    Oracle: the hand-written object.
    """
    raw = ('Looking at the existing memories, memory [0] is contradicted '
           'and the rest are unrelated.\n\n```json\n'
           '{"merged_text": null, "actions": [{"action": "SUPERSEDE", '
           '"target_id": 0, "reason": "x"}]}\n```')
    assert parse_json_response(raw) == {
        'merged_text': None,
        'actions': [{'action': 'SUPERSEDE', 'target_id': 0, 'reason': 'x'}]}


def test_parse_json_response_reads_the_last_object_of_a_self_correction():
    """Verify a "wait, let me reconsider" response is read at its final block.

    Mutation: returning the first object found, which is the answer the
        model withdrew.
    Oracle: the `attempt` marker that differs between the two blocks.
    """
    first = json.dumps({'attempt': 1, 'actions': [{'action': 'ADD'}]})
    second = json.dumps(
        {'attempt': 2, 'actions': [{'action': 'SUPERSEDE', 'target_id': 3}]})
    raw = (f'```json\n{first}\n```\n\nWait, I need to reconsider memory 3.'
           f'\n\n```json\n{second}\n```')
    assert parse_json_response(raw)['attempt'] == 2


def test_parse_json_response_repairs_a_lone_backslash():
    r"""Verify an invalid escape is repaired without touching valid pairs.

    Mutation: doubling every backslash (a valid `\\\\` pair becomes two
        characters), or leaving the lone one in place (the object fails
        to decode and the reader returns None).
    Oracle: the decoded string values: one backslash in each.
    """
    raw = '{"lone": "NT AUTHORITY\\SYSTEM", "pair": "C:\\\\Users"}'
    parsed = parse_json_response(raw)
    assert parsed == {'lone': 'NT AUTHORITY\\SYSTEM', 'pair': 'C:\\Users'}


def test_parse_json_list_response_reads_the_list_after_prose():
    """Verify a causal-edge list behind a paragraph of reasoning is still read.

    Mutation: trying only the whole text and the whole text with fences
        stripped, which returns None and drops every edge of the response.
    Oracle: the hand-written list.
    """
    raw = ('Memory [0] caused memory [1]; the rest are unrelated.\n\n```json\n'
           '[{"source_id": "a", "target_id": "b", "confidence": 0.9}]\n```')
    assert parse_json_list_response(raw) == [
        {'source_id': 'a', 'target_id': 'b', 'confidence': 0.9}]


def test_parse_json_list_response_prefers_the_list_of_objects():
    """Verify a bracketed index in trailing prose does not replace the edge list.

    Mutation: returning the last top-level list found, which is the `[1]`
        the model wrote while explaining itself.
    Oracle: the edge list, with the prose index left behind.
    """
    raw = ('[{"source_id": "a", "target_id": "b", "confidence": 0.9}]\n\n'
           'I linked [0] to [1] because the second describes the fix.')
    assert parse_json_list_response(raw) == [
        {'source_id': 'a', 'target_id': 'b', 'confidence': 0.9}]
