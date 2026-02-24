"""Breadth-first graph traversal."""

from collections import deque
from dataclasses import dataclass

from mnemon.store.edge import get_all_edges
from mnemon.store.node import get_all_active_insights


@dataclass
class BFSOptions:
    """Controls BFS traversal behavior."""

    max_depth: int = 2
    max_nodes: int = 0
    edge_filter: str = ''


def bfs(db: 'DB', start_id: str,
        opts: BFSOptions) -> list[dict]:
    """Perform breadth-first traversal from start_id over the full graph."""
    all_insights = get_all_active_insights(db)
    if not all_insights:
        return []

    insight_map = {ins.id: ins for ins in all_insights}

    all_edges = get_all_edges(db)
    edge_adj: dict[str, list] = {}
    for e in all_edges:
        edge_adj.setdefault(e.source_id, []).append(e)
        if e.source_id != e.target_id:
            edge_adj.setdefault(e.target_id, []).append(e)

    visited = {start_id}
    queue: deque[tuple[str, int]] = deque([(start_id, 0)])
    result = []

    while queue:
        if opts.max_nodes > 0 and len(result) >= opts.max_nodes:
            break

        cur_id, hop = queue.popleft()

        if hop >= opts.max_depth:
            continue

        for edge in edge_adj.get(cur_id, []):
            if opts.edge_filter and edge.edge_type != opts.edge_filter:
                continue

            neighbor_id = edge.target_id
            if neighbor_id == cur_id:
                neighbor_id = edge.source_id

            if neighbor_id in visited:
                continue
            visited.add(neighbor_id)

            ins = insight_map.get(neighbor_id)
            if ins is None:
                continue

            result.append({
                'insight': ins,
                'hop': hop + 1,
                'via_edge': edge.edge_type,
                })

            if opts.max_nodes > 0 and len(result) >= opts.max_nodes:
                break

            queue.append((neighbor_id, hop + 1))

    return result
