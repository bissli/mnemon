# 6. Lifecycle & Embedding

[< Back to Design Overview](../DESIGN.md)

---

Mnemon is not an append-only system. Effective memory management requires important memories to persist while outdated ones naturally decay.

![Lifecycle & Retention](../diagrams/06-lifecycle-retention.drawio.png)

## 6.1 Effective Importance (EI)

EI combines base importance, access frequency, time decay, and graph connectivity:

```
EI = base_weight(importance) × access_factor × decay_factor × edge_factor

base_weight:   imp 5 → 1.0,  4 → 0.8,  3 → 0.5,  2 → 0.3,  1 → 0.15
access_factor: max(1.0, log(1 + access_count))
decay_factor:  0.5 ^ (days_since_access / 30)     // half-life of 30 days
edge_factor:   1.0 + 0.1 × min(edge_count, 5)     // up to +0.5
```

Interpretation:
- **High importance** -> higher base score
- **Frequent access** -> logarithmic growth bonus
- **Long period without access** -> exponential decay (halves every 30 days)
- **Rich graph connections** -> indicates relevance to other knowledge, bonus applied

## 6.2 Immunity Rules

The following insights are exempt from automatic cleanup:
- `importance >= 4` (high-value memories)
- `access_count >= 3` (frequently retrieved)

## 6.3 Auto-Pruning

Triggered when the total number of active insights exceeds **1000**:

1. Compute EI for all insights
2. Exclude immune insights
3. Take the lowest EI entries in ascending order (up to 10 per batch)
4. Soft-delete (set `deleted_at`)
5. Cascade-delete related edges

## 6.4 GC Command

Manual lifecycle management tool:

```bash
# View low-retention candidates
mnemon gc --threshold 0.5

# Retain a specific insight (increases access_count by +3)
mnemon gc --keep <id>
```

---

## 6.5 Embedding Support

Embedding vectors are an optional enhancement. Without embeddings, Mnemon operates entirely on keywords and graph structure; with embeddings, semantic retrieval capabilities are significantly enhanced.

### Ollama Integration

Via the local Ollama service (no external API required):

```
Mnemon ──HTTP──→ Ollama (localhost:11434)
                  └── nomic-embed-text
                      768-dim vector
```

- **Availability detection**: 2-second timeout to avoid blocking
- **Graceful degradation**: Automatically falls back to token overlap when Ollama is unavailable
- **Zero new dependencies**: Pure stdlib `net/http`

### Vector Storage

Vectors are serialized as little-endian float64 BLOBs stored in the `insights.embedding` column (768 x 8 = 6144 bytes/insight).

### Usage Scenarios

| Scenario                   | Without Embedding     | With Embedding                   |
| -------------------------- | --------------------- | -------------------------------- |
| remember -> semantic edges | Token overlap > 0.10  | cos >= 0.80 auto-link            |
| recall -> anchors          | Keyword + recency     | Keyword + vector + recency       |
| recall -> traversal        | Pure structural score | Structural + semantic similarity |
| recall -> re-ranking       | KW + Entity + Graph   | KW + Entity + Similarity + Graph |

### Management Commands

```bash
ollama pull nomic-embed-text    # Install the model
mnemon embed --status           # View coverage
mnemon embed --all              # Batch-generate embeddings for all insights
mnemon embed <id>               # Generate for a single insight
```
