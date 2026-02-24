# 1. Vision & Problem

[< Back to Design Overview](../DESIGN.md)

---

## 1.1 Memory Is the Soul of an Agent

Without reliable long-term memory, an LLM agent can never evolve from a mere "tool" into a true "assistant."

The memory layer has a **compound interest effect** — the longer it is used, the more it accumulates, and the greater its value. It is the only component in the agent ecosystem that requires deep investment and cannot be replaced: LLM engines will continue to iterate (Anthropic/OpenAI/Google, etc.), Skills have near-zero marginal cost (just write markdown), but memory is a private asset that accumulates alongside the user over time.

## 1.2 The "Amnesia" Problem of LLMs

LLM agents suffer from three critical memory deficiencies:

- **Context compression loss**: After `/compact` or automatic compression, all prior decisions, discoveries, and context are lost
- **Cross-session forgetting**: Each new session starts from scratch, with no knowledge of previous sessions
- **Long-session decay**: Once the context window fills up, critical early information is pushed out of the attention range

For a digital assistant that needs to "continuously learn the user's thinking and become an extension of the user," these three deficiencies mean users must repeatedly restate preferences, re-explain project context, and re-derive conclusions already reached.

## 1.3 Structural Bottlenecks of Traditional Approaches

Existing RAG/Memory solutions have fundamental design limitations:

1. **Memory is an afterthought** — its lifecycle is tied to the agent session, not an independent entity
2. **Writing is reactive** — summaries are extracted after conversation ends, losing structural information
3. **Retrieval is flat** — relying solely on vector similarity, unable to express temporal/causal/contradictory relationships
4. **No forgetting mechanism** — either remember everything or TTL-based blanket expiration, no intelligent decay
5. **Heavy dependencies** — requires API keys, external databases, network connections

## 1.4 Mnemon's Mission

Mnemon's goal is: **to make an LLM remember your decisions, understand your preferences, and track project context like an experienced assistant — across arbitrarily many sessions.**

It is not a library or plugin embedded within an agent framework, but a standalone memory engine — callable via the command line by Claude Code, Cursor, or any LLM CLI.

## 1.5 Comparison with Alternatives

| Dimension            | Mem0                         | Letta/MemGPT           | Claude Code Memory            | **Mnemon**                          |
| -------------------- | ---------------------------- | ---------------------- | ----------------------------- | ----------------------------------- |
| **Architecture**     | SDK embedded in call chain   | Within agent framework | CLAUDE.md file injection      | Standalone Binary                   |
| **LLM Role**         | Internal extraction function | Agent self-managed     | None (static file load)       | External supervisor                 |
| **Graph**            | Neo4j single relation edges  | None                   | None                          | MAGMA four-graph                    |
| **Retrieval**        | Vector similarity            | Vector similarity      | Full-text loaded into context | Intent-adaptive multi-signal fusion |
| **External Deps**    | PostgreSQL + LLM API         | PostgreSQL + LLM API   | None                          | None                                |
| **LLM Swappable**    | Tied to OpenAI               | Tied to framework      | Claude Code only              | Any LLM CLI                         |
| **Memory Lifecycle** | Rules engine                 | No built-in decay      | Manual / auto-append          | EI decay + GC + immunity            |
