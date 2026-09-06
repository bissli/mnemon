"""JSON parsing helpers for LLM responses.

The LLM client class lives in `client.py`. HTTP retry/timeout policy
lives in `memman._http` (the single source of truth for HTTP policy
shared between LLM and embed paths).
"""

import json
import logging
import re

import httpx

logger = logging.getLogger('memman')

# Guardrail bounding pathological LLM output (a sentence or paragraph
# emitted as one entity/keyword), NOT a retrieval tunable -- it needs
# no ablation-harness sweep. Measured: the longest legitimate strings
# fleet-wide (cloud ARNs, directory DNs, Windows paths) reach 137
# chars, so 200 passes every observed legitimate value.
MAX_ENRICH_STRING_CHARS = 200


def drop_overlong_strings(
        values: list[str], *, kind: str, owner: str) -> list[str]:
    """Drop strings over `MAX_ENRICH_STRING_CHARS`, logging each drop.

    Parameters
    ----------
    values : list[str]
        LLM-proposed entities or keywords. Never pass user-supplied
        values -- `--entities` is uncapped by design.
    kind : str
        'entity' or 'keyword', for the drop log line.
    owner : str
        Insight id (or producer label) named in the drop log line.

    Returns
    -------
    list[str]
        The surviving values, order preserved.

    Notes
    -----
    - Drop, never truncate: a truncated entity is still a valid
      exact-match edge key and still lands in the embedding,
      preserving the pathology under a new name.
    - The cap is a guardrail bounding pathological LLM output, NOT a
      retrieval tunable: it needs no ablation-harness sweep. It is
      measured against the fleet's longest legitimate strings
      (137 chars), not tuned for retrieval quality.
    - Extraction-side drops are logged by content prefix, not
      insight id: no insight exists yet at extraction time, so the
      spec's log-the-id requirement is unmeetable there by
      construction.
    """
    kept = []
    for v in values:
        if len(v) > MAX_ENRICH_STRING_CHARS:
            logger.info(
                f'dropped over-long {kind} ({len(v)} chars) for'
                f' {owner}: {v[:40]!r}...')
            continue
        kept.append(v)
    return kept


def strip_code_fences(raw: str) -> str:
    """Strip markdown code fences from LLM output."""
    text = raw.strip()
    if text.startswith('```'):
        lines = text.split('\n')
        text = '\n'.join(lines[1:])
        text = text.removesuffix('```').strip()
    return text


# A valid escape pair is matched first, so a lone backslash is one that
# no escape character follows (`NT AUTHORITY\SYSTEM`); only it doubles.
_ESCAPE_RUN_RE = re.compile(r'\\\\|\\(?!["\\/bfnrtu])')


def _top_level_json_values(raw: str, opener: str) -> list:
    """Every JSON value that decodes from an `opener` in `raw`, in order.

    Parameters
    ----------
    raw : str
        The response text as the model returned it.
    opener : str
        `'{'` for objects, `'['` for lists.

    Returns
    -------
    list
        The decoded values. The scan resumes after each decoded value;
        an opener whose value fails to decode (a truncated outer
        object) is skipped by one character, so the values inside it
        decode on their own. The text is scanned as sent and, when
        nothing decodes, with lone backslashes repaired; a valid escape
        pair is never touched.
    """
    decoder = json.JSONDecoder()
    repaired = _ESCAPE_RUN_RE.sub(
        lambda m: '\\\\' if m.group(0) == '\\' else m.group(0), raw)
    for text in (raw, repaired) if repaired != raw else (raw,):
        found: list = []
        pos = 0
        while True:
            start = text.find(opener, pos)
            if start == -1:
                break
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                pos = start + 1
                continue
            found.append(value)
            pos = end
        if found:
            return found
    return []


def parse_json_response(raw: str) -> dict | None:
    """The JSON object an LLM response carries, or None.

    Parameters
    ----------
    raw : str
        The response text as the model returned it.

    Returns
    -------
    dict | None
        The whole text decoded as an object when it is one (fences
        stripped if present); else the LAST object the scan of
        `_top_level_json_values` finds, so a response that reasons
        before its JSON, or emits a block, says "let me reconsider" and
        emits another, is read at its final answer; None when no object
        decodes.

    Notes
    -----
    - When an enclosing object fails to decode (a response cut at the
      token ceiling), the objects inside it are what the scan finds;
      none carries the top-level key a caller reads, so the caller's
      failure path runs as before.
    """
    for text in (raw, strip_code_fences(raw)):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    objects = [v for v in _top_level_json_values(raw, '{') if isinstance(v, dict)]
    return objects[-1] if objects else None


def parse_json_list_response(raw: str) -> list | None:
    """The JSON list an LLM response carries, or None.

    Parameters
    ----------
    raw : str
        The response text as the model returned it.

    Returns
    -------
    list | None
        The whole text decoded as a list when it is one (fences stripped
        if present); else the last list of objects the scan finds, so a
        bracketed index the model wrote in its prose (`[0]`) never
        replaces the answer; else the last list of any shape; None when
        no list decodes.
    """
    for text in (raw, strip_code_fences(raw)):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    lists = [v for v in _top_level_json_values(raw, '[') if isinstance(v, list)]
    of_objects = [v for v in lists if all(isinstance(x, dict) for x in v)]
    chosen = of_objects or lists
    return chosen[-1] if chosen else None


def safe_json(resp: httpx.Response) -> object:
    """Return parsed JSON or the raw text if decoding fails."""
    try:
        return resp.json()
    except Exception:
        return resp.text
