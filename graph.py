"""In-memory exploration graph: screen-state nodes, action edges, and next-step selection."""
from __future__ import annotations

from dataclasses import dataclass, field

import config


@dataclass
class GraphNode:
    state_hash: str
    package: str
    activity: str
    actions: list[dict]
    tried_action_ids: set[str] = field(default_factory=set)

    def untried_actions(self) -> list[dict]:
        return [a for a in self.actions if a["id"] not in self.tried_action_ids]


@dataclass
class GraphEdge:
    from_hash: str
    to_hash: str
    action_id: str
    label: str


class ExplorationGraph:
    """Tracks visited screen states and picks the next action to keep exploration moving
    forward without looping — falling back to BACK when a state is exhausted."""

    def __init__(self):
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self._visit_order: list[str] = []
        self._consecutive_backtracks = 0

    # -- graph mutation -----------------------------------------------------------
    def upsert_node(self, state_hash: str, package: str, activity: str, actions: list[dict]) -> GraphNode:
        node = self.nodes.get(state_hash)
        if node is None:
            node = GraphNode(state_hash=state_hash, package=package, activity=activity, actions=actions)
            self.nodes[state_hash] = node
        else:
            # Refresh the action list (dynamic content may shift positions slightly) while
            # keeping the record of what's already been tried on this structural state.
            node.actions = actions
        if not self._visit_order or self._visit_order[-1] != state_hash:
            self._visit_order.append(state_hash)
        return node

    def record_edge(self, from_hash: str, to_hash: str, action_id: str, label: str) -> None:
        self.edges.append(GraphEdge(from_hash=from_hash, to_hash=to_hash, action_id=action_id, label=label))
        node = self.nodes.get(from_hash)
        if node is not None:
            node.tried_action_ids.add(action_id)

    def mark_tried(self, state_hash: str, action_id: str) -> None:
        node = self.nodes.get(state_hash)
        if node is not None:
            node.tried_action_ids.add(action_id)

    # -- next-step policy -----------------------------------------------------------
    def has_visited(self, state_hash: str) -> bool:
        return state_hash in self.nodes

    def is_exhausted(self, state_hash: str) -> bool:
        node = self.nodes.get(state_hash)
        return node is None or not node.untried_actions()

    def pick_next_action(self, state_hash: str) -> dict | None:
        """Return an untried action for this state, or None if it's fully explored
        (caller should backtrack with BACK)."""
        node = self.nodes.get(state_hash)
        if node is None:
            return None
        untried = node.untried_actions()
        if not untried:
            return None
        self._consecutive_backtracks = 0
        return untried[0]

    def should_force_stop(self) -> bool:
        """Guard against pathological BACK-BACK-BACK loops that never reach new UI."""
        return self._consecutive_backtracks >= config.MAX_CONSECUTIVE_BACKTRACKS

    def note_backtrack(self) -> None:
        self._consecutive_backtracks += 1

    def note_progress(self) -> None:
        self._consecutive_backtracks = 0

    # -- stats -----------------------------------------------------------
    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)
