"""Entity extraction (regex + tech dictionary) and entity edge creation."""

import math
import re
from datetime import datetime, timezone

from mnemon.model import Edge, Insight
from mnemon.store.edge import count_insights_with_entity
from mnemon.store.edge import find_insights_with_entity, insert_edge

MAX_ENTITY_LINKS = 5
MAX_TOTAL_ENTITY_EDGES = 50

ENTITY_PATTERNS = [
    re.compile(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b'),
    re.compile(r'\b([A-Z]{2,6})\b'),
    re.compile(r'(?:^|[\s"\'(])([.\w/-]+\.\w{1,10})(?:[\s"\'),.]|$)'),
    re.compile(r'https?://[^\s"\'<>)]+'),
    re.compile(r'@([a-zA-Z_]\w+)'),
    ]

TECH_DICTIONARY = {
    'Go', 'Rust', 'Python', 'Java', 'Kotlin', 'Swift', 'Ruby', 'Elixir',
    'Zig', 'Lua', 'Dart', 'Scala', 'Perl', 'Haskell', 'OCaml', 'Julia',
    'Clojure', 'JavaScript', 'TypeScript', 'React', 'Vue', 'Angular',
    'Svelte', 'Next', 'Nuxt', 'Node', 'Deno', 'Bun', 'Vite', 'Webpack',
    'SQLite', 'PostgreSQL', 'Postgres', 'MySQL', 'Redis', 'MongoDB',
    'DynamoDB', 'Cassandra', 'Qdrant', 'Milvus', 'Chroma', 'Pinecone',
    'Neo4j', 'Weaviate', 'Elasticsearch', 'Docker', 'Kubernetes',
    'Terraform', 'Ansible', 'Nginx', 'Caddy', 'Kafka', 'RabbitMQ',
    'AWS', 'GCP', 'Azure', 'Vercel', 'Netlify', 'Cloudflare', 'Supabase',
    'Firebase', 'Ollama', 'OpenAI', 'Claude', 'Anthropic', 'PyTorch',
    'TensorFlow', 'LangChain', 'LlamaIndex', 'FAISS', 'Hugging', 'Git',
    'GitHub', 'GitLab', 'Cobra', 'FastAPI', 'Flask', 'Django', 'Rails',
    'Spring', 'Express', 'Gin', 'Echo', 'Fiber', 'Pytest', 'Jest',
    'Vitest', 'gRPC', 'GraphQL', 'WebSocket', 'OAuth', 'JWT', 'YAML',
    'TOML', 'Protobuf', 'MAGMA', 'MCP', 'RLM',
    }

ACRONYM_STOPWORDS = {
    'IN', 'ON', 'AT', 'TO', 'BY', 'OR', 'AN', 'IF', 'IS', 'IT',
    'OF', 'AS', 'DO', 'NO', 'SO', 'UP', 'WE', 'HE', 'MY', 'BE',
    'GO', 'THE', 'AND', 'FOR', 'ARE', 'BUT', 'NOT', 'YOU', 'ALL',
    'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'HAS', 'HAD', 'HOW',
    'MAN', 'NEW', 'NOW', 'OLD', 'SEE', 'WAY', 'MAY', 'SAY', 'SHE',
    'TWO', 'USE', 'BOY', 'DID', 'GET', 'HIM', 'HIS', 'LET', 'PUT',
    'TOP', 'TOO', 'ANY',
    }

_WORD_SPLIT_RE = re.compile(r'[a-zA-Z0-9]+')


def split_words(text: str) -> list[str]:
    """Split text into ASCII-alphanumeric words preserving original casing."""
    return _WORD_SPLIT_RE.findall(text)


def extract_entities(text: str) -> list[str]:
    """Extract named entities from text using regex patterns and tech dictionary."""
    seen: set[str] = set()
    entities: list[str] = []

    for pat in ENTITY_PATTERNS:
        for m in pat.finditer(text):
            entity = m.group(m.lastindex or 0)
            if not entity or entity in seen:
                continue
            if entity in ACRONYM_STOPWORDS:
                continue
            seen.add(entity)
            entities.append(entity)

    for word in split_words(text):
        if word in TECH_DICTIONARY and word not in seen:
            seen.add(word)
            entities.append(word)

    return entities


def merge_entities(
        provided: list[str],
        extracted: list[str]) -> list[str]:
    """Deduplicate and merge pre-provided with regex-extracted entities."""
    seen: set[str] = set()
    merged: list[str] = []
    for e in provided:
        if e and e not in seen:
            seen.add(e)
            merged.append(e)
    for e in extracted:
        if e and e not in seen:
            seen.add(e)
            merged.append(e)
    return merged


def entity_idf_weight(doc_freq: int, total_docs: int) -> float:
    """Compute IDF-based weight for an entity edge."""
    if total_docs <= 1 or doc_freq >= total_docs:
        return 0.0
    if doc_freq <= 0:
        return 1.0
    raw = math.log(total_docs / doc_freq) / math.log(total_docs)
    return max(raw, 0.1)


def create_entity_edges(db: 'DB', insight: Insight) -> int:
    """Create entity co-occurrence edges between the insight and existing insights."""
    if not insight.entities:
        return 0

    from mnemon.store.node import count_active_insights
    total_docs = count_active_insights(db)
    use_idf = total_docs > 5

    now = datetime.now(timezone.utc)
    count = 0

    for entity in insight.entities:
        if count >= MAX_TOTAL_ENTITY_EDGES:
            break
        ids = find_insights_with_entity(
            db, entity, insight.id, MAX_ENTITY_LINKS)
        if not ids:
            continue

        if use_idf:
            doc_freq = count_insights_with_entity(
                db, entity, insight.id) + 1
            weight = entity_idf_weight(doc_freq, total_docs)
            if weight == 0.0:
                continue
        else:
            weight = 1.0

        for target_id in ids:
            if count >= MAX_TOTAL_ENTITY_EDGES:
                break
            try:
                insert_edge(db, Edge(
                    source_id=insight.id, target_id=target_id,
                    edge_type='entity', weight=weight,
                    metadata={'entity': entity}, created_at=now))
                count += 1
            except Exception:
                pass
            try:
                insert_edge(db, Edge(
                    source_id=target_id, target_id=insight.id,
                    edge_type='entity', weight=weight,
                    metadata={'entity': entity}, created_at=now))
                count += 1
            except Exception:
                pass

    return count
