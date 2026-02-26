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

**Rationale:**

- **`base_weight` (1.0, 0.8, 0.5, 0.3, 0.15)**: Non-linear spacing creates a 6.7:1 ratio between importance 5 and 1. The largest gap (0.3→0.15) falls between importance 2→1, while the 0.8→0.5 gap between importance 4→3 reinforces the immunity boundary — importance 4+ is the "protected" tier.
- **`HALF_LIFE_DAYS = 30`**: One calendar month. Balances retention vs decay across typical project lifecycles: at 30 days EI halves, at 60 days quarters, at 90 days ~12.5%. Inspired by Ebbinghaus forgetting curve research but not derived from a specific paper — chosen as a round-number approximation for monthly project cadence. Not from MAGMA (the paper has no decay mechanism).
- **`edge_factor`: cap at 5 edges, +0.1 per edge (max +50%)**: Prevents highly-connected hub nodes from becoming permanently immune through connectivity alone.

## 6.2 Immunity Rules

The following insights are exempt from automatic cleanup:
- `importance >= 4` (high-value memories)
- `access_count >= 3` (frequently retrieved)

**Rationale:**

- **`importance >= 4`**: Follows directly from the importance scale definition — importance 4 = "immune to auto-pruning" (Section 3.1).
- **`access_count >= 3`**: Three independent retrievals provide statistical evidence of genuine utility, not coincidental access. The threshold is deliberately low — in a personal memory system, even two recalls suggest real value, but three provides a safety margin.

## 6.3 Auto-Pruning

Triggered when the total number of active insights exceeds **1000**:

1. Compute EI for all insights
2. Exclude immune insights
3. Take the lowest EI entries in ascending order (up to 10 per batch)
4. Soft-delete (set `deleted_at`)
5. Cascade-delete related edges

**Rationale:**

- **`MAX_INSIGHTS = 1000`**: Practical capacity for a single-user CLI memory system. Keeps SQLite scan cost bounded. Not from MAGMA (the paper specifies no storage capacity limit; its `Max Nodes: 200` is a per-query traversal budget, a different concept).
- **`PRUNE_BATCH_SIZE = 10`**: ~1% of MAX_INSIGHTS. Limits write amplification per `remember` call — a single insert never cascades into mass deletion.
- **`MAX_OPLOG_ENTRIES = 5000`**: 5× MAX_INSIGHTS; retains approximately five operations per insight on average. Sufficient audit trail without unbounded growth.

## 6.4 GC Command

Manual lifecycle management tool:

```bash
# View low-retention candidates
mnemon gc --threshold 0.5

# Retain a specific insight (increases access_count by +3)
mnemon gc --keep <id>

# Review stored insights for content quality issues
mnemon gc --review
```

`gc --review` scans all active insights against transient content patterns (AWS instance IDs, resource counts, verification receipts, deployment receipts, state observations). Returns flagged entries sorted by warning count. Aligns with MAGMA's slow-path philosophy: the fast path (remember) stores quickly with advisory warnings; the slow path (gc --review) enables async quality review.

**Rationale:**

- **`boost_retention +3`**: Deliberately matches the immunity threshold (`access_count >= 3`). A single `gc --keep` guarantees immunity regardless of prior access count — the insight crosses the threshold immediately.

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
