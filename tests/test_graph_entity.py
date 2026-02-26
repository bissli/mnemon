"""Entity extraction, merging, and IDF weighting tests."""

from mnemon.graph.entity import entity_idf_weight
from mnemon.graph.entity import extract_entities, merge_entities, split_words


class TestExtractCamelCase:
    """CamelCase identifiers extracted as entities."""

    def test_extract_camelcase(self):
        """HttpServer and MyClient recognized as camelCase entities."""
        entities = extract_entities('HttpServer handles requests via MyClient')
        assert 'HttpServer' in entities
        assert 'MyClient' in entities


class TestExtractAcronyms:
    """Uppercase acronyms (2-6 chars) extracted."""

    def test_extract_acronyms(self):
        """API, HTTP, SQL recognized as acronym entities."""
        entities = extract_entities('The API uses HTTP and SQL')
        assert 'API' in entities
        assert 'HTTP' in entities
        assert 'SQL' in entities


class TestExtractAcronymStopwords:
    """Common English uppercase words filtered out."""

    def test_extract_acronym_stopwords(self):
        """IT, IS, IN filtered as acronym stopwords."""
        entities = extract_entities('IT IS IN the system')
        assert 'IT' not in entities
        assert 'IS' not in entities
        assert 'IN' not in entities


class TestExtractURLs:
    """URLs extracted as entities."""

    def test_extract_urls(self):
        """Full URL recognized as entity."""
        entities = extract_entities(
            'Check https://github.com/user/repo for details')
        found = any('https://github.com/user/repo' in e for e in entities)
        assert found


class TestExtractMentions:
    """@mentions extracted as entities."""

    def test_extract_mentions(self):
        """@johndoe and @alice extracted."""
        entities = extract_entities('Thanks @johndoe and @alice for the review')
        assert 'johndoe' in entities
        assert 'alice' in entities


class TestExtractTechDictionary:
    """Words in the tech dictionary recognized."""

    def test_extract_tech_dict(self):
        """Go, SQLite, Docker found via tech dictionary lookup."""
        entities = extract_entities(
            'We use Go with SQLite and deploy via Docker')
        assert 'Go' in entities
        assert 'SQLite' in entities
        assert 'Docker' in entities


class TestExtractFilePaths:
    """File paths with extensions extracted."""

    def test_extract_file_paths(self):
        """cmd/root.go recognized as file path entity."""
        entities = extract_entities('Edit cmd/root.go to add the command')
        found = any('cmd/root.go' in e for e in entities)
        assert found


class TestExtractEmpty:
    """Empty input yields empty entity list."""

    def test_extract_empty(self):
        """Empty string produces no entities."""
        entities = extract_entities('')
        assert entities == []


class TestNoDuplicates:
    """Duplicate entities collapsed to single occurrence."""

    def test_no_duplicates(self):
        """API mentioned twice appears only once in output."""
        entities = extract_entities('API calls the API endpoint')
        count = sum(1 for e in entities if e == 'API')
        assert count == 1


class TestMergeBasic:
    """Provided entities come first, extracted deduplicated."""

    def test_merge_basic(self):
        """Provided list precedes extracted; shared items not duplicated."""
        merged = merge_entities(['Go', 'Docker'], ['Docker', 'SQLite'])
        assert merged[0] == 'Go'
        assert merged[1] == 'Docker'
        assert 'SQLite' in merged
        assert len(merged) == 3


class TestMergeBothEmpty:
    """Two empty lists produce empty list, not None."""

    def test_merge_both_empty(self):
        """Merging two empty lists returns empty list."""
        merged = merge_entities([], [])
        assert merged is not None
        assert merged == []


class TestMergeEmptyStringsFiltered:
    """Empty strings in input lists are filtered out."""

    def test_merge_empty_strings_filtered(self):
        """Empty string entries stripped from both provided and extracted."""
        merged = merge_entities(['', 'Go'], ['', 'Docker'])
        assert '' not in merged
        assert 'Go' in merged
        assert 'Docker' in merged


class TestSplitWords:
    """split_words extracts ASCII alphanumeric tokens."""

    def test_split_words(self):
        """Splits on non-alphanumeric boundaries, preserves casing."""
        words = split_words('Hello world-foo_bar 42')
        assert 'Hello' in words
        assert 'world' in words
        assert 'foo' in words
        assert 'bar' in words
        assert '42' in words


class TestEntityIdfWeightRare:
    """Rare entity (doc_freq=1) gets maximum weight."""

    def test_rare_entity(self):
        """Entity in 1 of 100 docs yields weight ~1.0."""
        w = entity_idf_weight(1, 100)
        assert abs(w - 1.0) < 0.01


class TestEntityIdfWeightCommon:
    """Common entity gets low weight."""

    def test_common_entity(self):
        """Entity in 80 of 100 docs yields weight < 0.2."""
        w = entity_idf_weight(80, 100)
        assert w < 0.2


class TestEntityIdfWeightUniversal:
    """Universal entity (in all docs) gets zero weight."""

    def test_universal_entity(self):
        """Entity in all docs yields weight 0.0."""
        w = entity_idf_weight(100, 100)
        assert w == 0.0


class TestEntityIdfWeightEdgeCases:
    """Edge cases for IDF weight computation."""

    def test_single_doc(self):
        """Only 1 total doc yields 0.0."""
        w = entity_idf_weight(1, 1)
        assert w == 0.0

    def test_zero_doc_freq(self):
        """doc_freq=0 (brand new entity) yields maximum weight."""
        w = entity_idf_weight(0, 100)
        assert w > 0.9

    def test_floor(self):
        """Weight never drops below 0.1 for non-universal entities."""
        w = entity_idf_weight(90, 100)
        assert w >= 0.1

    def test_monotonic(self):
        """Weight decreases as doc_freq increases."""
        w1 = entity_idf_weight(2, 100)
        w2 = entity_idf_weight(50, 100)
        w3 = entity_idf_weight(90, 100)
        assert w1 > w2 > w3
