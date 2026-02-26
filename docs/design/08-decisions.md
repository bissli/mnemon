# 8. Design Decisions & Future Direction

[< Back to Design Overview](../DESIGN.md)

---

## 8.1 Design Decisions & Trade-offs

### Why LLM-Supervised Instead of an Embedded LLM?

| Dimension          | LLM-Embedded (Mem0, etc.)     | LLM-Supervised (Mnemon) |
| ------------------ | ----------------------------- | ----------------------- |
| LLM Capability     | gpt-4o-mini (constrained)     | Host LLM (Opus-class)   |
| API Cost           | Every operation incurs a call | Zero                    |
| Network Dependency | Required                      | Not required            |
| Swappability       | API-bound                     | Any LLM CLI             |

### Why SQLite WAL Instead of an Embedded Graph Database?

- **Single-file deployment**: one `.db` file per store — easy to manage and backup
- **ACID transactions**: Atomicity guarantee for the remember pipeline
- **WAL concurrency**: Supports simultaneous hook reads and CLI writes
- **Zero external dependencies**: No Redis/Neo4j/Qdrant required
- **Store isolation**: Named stores (`~/.mnemon/data/<name>/mnemon.db`) provide lightweight data isolation via `MNEMON_STORE` env var

### Why Beam Search Instead of Full BFS?

- **Budget control**: MaxVisited parameter prevents graph explosion
- **Intent-adaptive**: Different intents use different beam widths and depths
- **Quality assurance**: Only the highest-scoring candidates are retained at each level, similar to pruning

### Why Soft Delete?

- Preserves audit trail
- Supports "undo" (recovering accidental deletions)
- Simplifies cascade cleanup
- Query consistency (`WHERE deleted_at IS NULL`)

### Key Deviations from the MAGMA Paper

| Aspect            | MAGMA Paper                        | Mnemon Implementation                                         |
| ----------------- | ---------------------------------- | ------------------------------------------------------------- |
| Entity Extraction | LLM-driven full pipeline           | Regex + dictionary + LLM supplementation; IDF-weighted edges  |
| Causal Reasoning  | Embedded prompt chain              | Auto candidates + LLM review                                  |
| Node Types        | EVENT, EPISODE, SESSION, NARRATIVE | Insight only (flat)                                           |
| Storage           | NetworkX (in-memory)               | SQLite (persistent)                                           |
| Embeddings        | FAISS + OpenAI                     | Ollama (local, optional)                                      |
| Quality Review    | Slow-path LLM refinement (Alg. 3)  | Pattern-based quality warnings (remember) + async gc --review |
| Deployment        | Python library                     | Python package (CLI)                                          |

Mnemon retains MAGMA's **architectural skeleton** (four-graph separation, intent-adaptive retrieval, multi-signal fusion) while replacing academic implementation details with production-ready simplifications. This two-tier approach — deterministic automation for the majority of cases, LLM judgment for the complex minority — is precisely the pattern validated by the [RLM paper](02-philosophy.md#25-theoretical-foundations): regex-based filtering plus LLM-driven semantic verification consistently outperforms either approach alone. The core trade-off is: **use regex/heuristics to handle 80% of automation scenarios, and delegate the 20% requiring deep understanding to the host LLM.**

---

## 8.2 Future Direction

The [two-layer architecture](02-philosophy.md#23-memory-gateway-protocol-not-database) has achieved agent-side pluggability — any LLM CLI can interact with Mnemon through the protocol surface today. The remaining work is on the other side.

### Storage-Side Pluggability

The storage engine is currently tightly built on SQLite — graph traversal, EI decay, and atomic transactions all depend on SQLite-specific features (WAL, single-file deployment, in-process access). This is the right choice for the current goal of zero-dependency single-package distribution, but it means the storage backend is not yet swappable.

Abstracting the storage interface — so the protocol layer can sit on top of PostgreSQL, a dedicated graph database, or a remote service — is the next architectural milestone. The protocol naturally accommodates different backends with varying expressiveness:

```
              remember        link                recall
              ─────────       ────────────────     ──────────────────
Neo4j         CREATE node     CREATE edge          MATCH + traverse
TigerGraph    add vertex      add edge             GSQL query
Milvus        upsert vec      metadata ref         ANN search
PostgreSQL    INSERT row      INSERT FK/join        SELECT + JOIN
Redis         SET key         _(degenerate)_        GET key
SQLite        INSERT row      INSERT edge table     multi-signal query
```

Graph databases implement the protocol most naturally — all three primitives map directly to native operations. Relational databases need a translation layer for `link` (foreign keys are fixed at schema design time, not dynamically created). KV stores can only implement `remember` + `recall` (`link` degenerates). This spectrum reflects the [structural insight](04-graph-model.md#other-storage-types-as-degenerate-graphs) that other storage types are degenerate forms of graphs.

The key challenge is defining the right abstraction boundary: too high and you lose the storage engine's graph-aware optimizations; too low and every backend must reimplement Beam Search and RRF fusion.

### Toward a Memory Gateway

When both boundaries are decoupled, Mnemon becomes a true memory gateway — any LLM on top, any storage backend underneath, with the protocol layer as the stable contract between them:

```
         Monolithic systems              Protocol gateway
         (product approach)              (platform approach)

Mem0  ──┐                         ┌── Neo4j adapter
CC Mem──┤ Each reinvents its      │── TigerGraph adapter
Viking──┤ own storage layer       │── Milvus adapter
MemGPT──┘                         │── SQLite adapter (current)
                                   └── PostgreSQL adapter

                                   ↑
                              Mnemon's position:
                              not another database,
                              but the LLM ↔ DB protocol gateway
```

This reframes Mnemon's competitive position:

- **Not competing with Neo4j** on storage engines — DB problems belong to DB
- **Not competing with Mem0** on product features — Mem0 is a product bound to its own storage implementation
- **Analogous to MCP** — MCP connected LLMs to the tool ecosystem; this protocol connects LLMs to the database ecosystem, especially graph databases where the three primitives achieve their most complete expression

The three properties that make this viable:

- **Agent-side optimization** (when to recall, what to remember, how to evaluate candidates) and **storage-side optimization** (indexing, query planning, graph algorithms) evolve independently
- The protocol surface — `remember`, `link`, `recall`, lifecycle hooks, structured JSON with signal transparency — remains the stable interface that both sides program against
- The [universal algebra](04-graph-model.md#remember--link--recall-as-universal-algebra) of `remember / link / recall` ensures this interface is not arbitrary but reflects the minimal complete set of primitives for any agent memory system
