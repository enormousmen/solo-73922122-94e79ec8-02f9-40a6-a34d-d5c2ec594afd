"""Recursive static-route resolution into /compress-ready forwarding entries.

Input model
-----------
* ``connected``: directly attached IPv4 prefixes, each pinned to an egress
  interface.  A gateway address inside one of them is *on-link* and the
  longest matching connected prefix wins.
* ``static``: static routes (prefix -> next-hop IPv4 address).  A gateway
  that is not on-link is followed through the static table: every static
  route whose prefix covers the gateway is a candidate, tried from the
  longest prefix downwards (LPM order).

Resolution of one static route starts at *its own next-hop* and follows the
dependency chain until a gateway lands on a connected prefix.

Cycles / explosion safety
-------------------------
The candidate graph has a node per static prefix and edges to the covering
routes of each next-hop (plus a terminal edge when the next-hop is on-link).

1. A reverse reachability flood from every on-link terminal marks the
   *resolvable* nodes.  Nodes not reached are provably unable to ever hit a
   connected prefix — including every node that is part of, or only leads
   to, a loop — and are answered without any path enumeration.
2. The actual chain is recovered with DFS that prunes any branch that would
   visit a node already on the current dependency path (a repeated route
   means a loop on that branch).  Two sound memos — validated chain reuse
   and context-subset failure reuse — keep the work polynomial; every
   possible chain is never enumerated.

The first LPM-ordered chain that reaches a connected prefix is returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from typing import Dict, List, Optional, Tuple

# Reason codes for unresolvable prefixes.
LOOP = "loop"          # part of a cycle, or every branch closes a loop
NO_ROUTE = "no_route"  # some gateway matches neither connected nor static route


@dataclass(frozen=True)
class Hop:
    """One step of a dependency chain: a route prefix and its next-hop."""

    prefix: str
    gateway: str


@dataclass
class ResolvedRoute:
    prefix: str
    chain: List[Hop]
    interface: str
    final_gateway: str
    connected_prefix: str
    nexthop_id: str
    compress_route: dict = field(default_factory=dict)


@dataclass
class UnresolvedRoute:
    prefix: str
    gateway: str
    reason: str


def make_nexthop_id(interface: str, gateway: str) -> str:
    """Next-hop identifier used as the ``action`` of a /compress route."""
    return f"{interface}:{gateway}"


class _Resolver:
    """Graph construction, reachability marking and LPM-first chain DFS."""

    def __init__(
        self,
        connected: List[Tuple[IPv4Network, str]],
        static: List[Tuple[IPv4Network, IPv4Address]],
    ):
        self.connected = connected  # distinct prefixes
        self.static = static
        self.static_by_net = {net: gw for net, gw in static}

        # node index per static prefix
        self.nets: List[IPv4Network] = [net for net, _ in static]
        self.index = {net: i for i, net in enumerate(self.nets)}
        self.n = len(self.nets)

        # adjacency, built lazily per gateway address:
        #   terminal   -> ("T", interface, connected_prefix)
        #   static edge-> ("S", node_index)
        self._edge_cache: Dict[int, list] = {}
        self.good: Optional[List[bool]] = None

        # DFS memos (single resolver instance = one request)
        self._ok_memo: Dict[int, List[int]] = {}     # node -> node-index chain
        self._fail_memo: Dict[int, frozenset] = {}   # node -> blocking path set

    # ------------------------------------------------------------------
    # candidate edges
    # ------------------------------------------------------------------

    def _edges_for_gateway(self, gw: IPv4Address) -> list:
        """LPM-ordered candidates for a gateway address.

        Longest connected prefix first (terminal); otherwise the covering
        static routes from the longest prefix down.  Distinct prefixes make
        every prefix length unique among covers, so there are no ties.
        """
        key = int(gw)
        cached = self._edge_cache.get(key)
        if cached is not None:
            return cached

        covers = [
            net for (net, _iface) in self.connected
            if int(net.network_address) <= key <= int(net.broadcast_address)
        ]
        edges: list = []
        if covers:
            net = max(covers, key=lambda n: n.prefixlen)  # longest match
            iface = next(iface for n, iface in self.connected if n == net)
            edges.append(("T", iface, str(net)))
        else:
            statics = [
                net
                for net in self.nets
                if int(net.network_address) <= key <= int(net.broadcast_address)
            ]
            statics.sort(key=lambda n: n.prefixlen, reverse=True)
            edges = [("S", self.index[net]) for net in statics]

        self._edge_cache[key] = edges
        return edges

    def _build_reverse_graph(self):
        """Reverse adjacency over static nodes plus the set of terminal nodes."""
        rev: List[List[int]] = [[] for _ in range(self.n)]
        terminal_nodes: List[int] = []
        for i, (_net, gw) in enumerate(self.static):
            for edge in self._edges_for_gateway(gw):
                if edge[0] == "T":
                    terminal_nodes.append(i)
                else:
                    rev[edge[1]].append(i)
        return rev, terminal_nodes

    def mark_resolvable(self) -> List[bool]:
        """Reverse flood: a node is good when it can reach an on-link terminal."""
        rev, terminals = self._build_reverse_graph()
        good = [False] * self.n
        stack = list(terminals)
        for t in terminals:
            good[t] = True
        while stack:
            node = stack.pop()
            for parent in rev[node]:
                if not good[parent]:
                    good[parent] = True
                    stack.append(parent)
        self.good = good
        return good

    # ------------------------------------------------------------------
    # chain recovery
    # ------------------------------------------------------------------

    def _dfs(self, node: int, on_path: frozenset) -> Optional[List[int]]:
        """First LPM-ordered node chain from ``node`` to a terminal.

        Returns the list of static-node indices ending at the node whose
        next-hop is on-link, or None when this context cannot reach one.
        Only nodes marked good are ever entered; ``on_path`` prunes loops.
        """
        if node in on_path:
            return None

        # reuse a previously found chain only if none of its nodes is busy
        cached_ok = self._ok_memo.get(node)
        if cached_ok is not None and all(j not in on_path for j in cached_ok):
            return cached_ok

        # a cached failure holds when the path that blocked it is still present
        cached_fail = self._fail_memo.get(node)
        if cached_fail is not None and cached_fail <= on_path:
            return None

        gw = self.static_by_net[self.nets[node]]
        new_path = on_path | {node}
        for edge in self._edges_for_gateway(gw):
            if edge[0] == "T":
                chain = [node]
                self._ok_memo[node] = chain
                return chain
            nxt = edge[1]
            if not self.good[nxt] or nxt in new_path:
                continue
            sub = self._dfs(nxt, new_path)
            if sub is not None:
                chain = [node] + sub
                self._ok_memo[node] = chain
                return chain

        self._fail_memo[node] = new_path
        return None

    def resolve(self):
        self.mark_resolvable()
        loop_nodes = self._loop_nodes()
        resolved: List[ResolvedRoute] = []
        unresolved: List[UnresolvedRoute] = []

        for i, (net, gw) in enumerate(self.static):
            if not self.good[i]:
                reason = LOOP if i in loop_nodes else NO_ROUTE
                unresolved.append(
                    UnresolvedRoute(prefix=str(net), gateway=str(gw), reason=reason)
                )
                continue

            idxs = self._dfs(i, frozenset())
            if idxs is None:  # pragma: no cover - good nodes always resolve
                unresolved.append(
                    UnresolvedRoute(prefix=str(net), gateway=str(gw), reason=LOOP)
                )
                continue

            last = idxs[-1]
            term = next(
                e for e in self._edges_for_gateway(self.static_by_net[self.nets[last]])
                if e[0] == "T"
            )
            _kind, interface, connected_prefix = term
            final_gateway = str(self.static_by_net[self.nets[last]])
            chain = [
                Hop(prefix=str(self.nets[j]), gateway=str(self.static_by_net[self.nets[j]]))
                for j in idxs
            ]
            nexthop_id = make_nexthop_id(interface, final_gateway)
            resolved.append(
                ResolvedRoute(
                    prefix=str(net),
                    chain=chain,
                    interface=interface,
                    final_gateway=final_gateway,
                    connected_prefix=connected_prefix,
                    nexthop_id=nexthop_id,
                    compress_route={"prefix": str(net), "action": nexthop_id},
                )
            )
        return resolved, unresolved

    def _loop_nodes(self) -> set:
        """Non-good nodes whose dependency walk can only end in a loop.

        A self-loop or a multi-node SCC in the non-good subgraph is a real
        routing loop; any non-good node that can reach such an SCC (and can
        never reach a terminal, since it is non-good) is reported as a loop
        too.  Remaining non-good nodes dead-end on an unmatched gateway.
        """
        # iterative Tarjan over edges restricted to non-good nodes
        index_of = {}
        low = {}
        on_stack = set()
        stack: List[int] = []
        scc_members: List[set] = []
        counter = 0

        for start in range(self.n):
            if self.good[start] or start in index_of:
                continue
            work = [(start, 0)]
            while work:
                node, pi = work[-1]
                if pi == 0:
                    index_of[node] = counter
                    low[node] = counter
                    counter += 1
                    stack.append(node)
                    on_stack.add(node)
                neighbors = [
                    e[1]
                    for e in self._edges_for_gateway(self.static_by_net[self.nets[node]])
                    if e[0] == "S" and not self.good[e[1]]
                ]
                if pi < len(neighbors):
                    work[-1] = (node, pi + 1)
                    nxt = neighbors[pi]
                    if nxt not in index_of:
                        work.append((nxt, 0))
                    elif nxt in on_stack:
                        low[node] = min(low[node], index_of[nxt])
                else:
                    if low[node] == index_of[node]:
                        comp = set()
                        while True:
                            w = stack.pop()
                            on_stack.discard(w)
                            comp.add(w)
                            if w == node:
                                break
                        scc_members.append(comp)
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        low[parent] = min(low[parent], low[node])

        cyclic = set()
        for comp in scc_members:
            if len(comp) > 1:
                cyclic |= comp
                continue
            (node,) = comp
            # self-loop: the node's own gateway covers the node's prefix
            for e in self._edges_for_gateway(self.static_by_net[self.nets[node]]):
                if e[0] == "S" and e[1] == node:
                    cyclic.add(node)
                    break

        # propagate backwards: any non-good node that can reach a loop is
        # itself loop-bound (it has no terminal path by definition)
        rev_bad: Dict[int, List[int]] = {}
        for node in range(self.n):
            if self.good[node]:
                continue
            for e in self._edges_for_gateway(self.static_by_net[self.nets[node]]):
                if e[0] == "S" and not self.good[e[1]]:
                    rev_bad.setdefault(e[1], []).append(node)
        work = list(cyclic)
        while work:
            node = work.pop()
            for parent in rev_bad.get(node, ()):
                if parent not in cyclic:
                    cyclic.add(parent)
                    work.append(parent)
        return cyclic


def resolve_routes(connected, static):
    """Resolve recursive static routes.

    ``connected``: list of (IPv4Network, interface_name).
    ``static``: list of (IPv4Network, IPv4Address) in input order.

    Returns (resolved, unresolved): lists of :class:`ResolvedRoute` and
    :class:`UnresolvedRoute`, both in static-route input order.
    """
    return _Resolver(connected, static).resolve()
