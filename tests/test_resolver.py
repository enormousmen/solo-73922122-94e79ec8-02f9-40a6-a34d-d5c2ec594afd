"""Tests for recursive static-route resolution.

Covers: direct termination, connected longest-prefix match, LPM-ordered
recursion, loop pruning with fallback, dead ends, self loops, a brute-force
randomized oracle (small cases), and worst-case 128-route stress cases that
must stay fast without enumerating chains.
"""

import random
import time
from ipaddress import IPv4Address, IPv4Network

from app import resolver as R
from app.resolver import LOOP, NO_ROUTE, resolve_routes


def C(prefix, iface):
    return (IPv4Network(prefix), iface)


def S(prefix, gateway):
    return (IPv4Network(prefix), IPv4Address(gateway))


def chain_prefixes(route):
    return [h.prefix for h in route.chain]


# ---------------------------------------------------------------------------
# crafted cases
# ---------------------------------------------------------------------------

def test_directly_connected_gateway_single_hop():
    resolved, unresolved = resolve_routes(
        [C("192.0.2.0/24", "eth0")],
        [S("203.0.113.0/24", "192.0.2.2")],
    )
    assert unresolved == []
    (r,) = resolved
    assert r.prefix == "203.0.113.0/24"
    assert chain_prefixes(r) == ["203.0.113.0/24"]
    assert r.chain[0].gateway == "192.0.2.2"
    assert r.interface == "eth0"
    assert r.final_gateway == "192.0.2.2"
    assert r.connected_prefix == "192.0.2.0/24"
    assert r.nexthop_id == "eth0:192.0.2.2"
    assert r.compress_route == {"prefix": "203.0.113.0/24", "action": "eth0:192.0.2.2"}


def test_connected_longest_prefix_match():
    # 192.168.10.200 belongs to both the /24 and the /25: /25 wins
    resolved, unresolved = resolve_routes(
        [C("192.168.10.0/24", "eth1"), C("192.168.10.128/25", "eth2")],
        [S("172.18.0.0/16", "192.168.10.200")],
    )
    (r,) = resolved
    assert r.interface == "eth2"
    assert r.connected_prefix == "192.168.10.128/25"
    assert r.final_gateway == "192.168.10.200"

    # an address only in the /24 takes the shorter prefix
    resolved2, _ = resolve_routes(
        [C("192.168.10.0/24", "eth1"), C("192.168.10.128/25", "eth2")],
        [S("172.18.0.0/16", "192.168.10.10")],
    )
    assert resolved2[0].interface == "eth1"
    assert resolved2[0].connected_prefix == "192.168.10.0/24"


def test_recursive_chain_order_and_terminal_gateway():
    connected = [C("10.0.0.0/8", "eth0")]
    static = [
        S("203.0.113.0/24", "198.51.100.2"),
        S("198.51.100.0/24", "192.0.2.9"),
        S("192.0.2.0/24", "10.1.1.1"),
    ]
    resolved, unresolved = resolve_routes(connected, static)
    assert unresolved == []
    by_prefix = {r.prefix: r for r in resolved}
    r = by_prefix["203.0.113.0/24"]
    assert chain_prefixes(r) == [
        "203.0.113.0/24",
        "198.51.100.0/24",
        "192.0.2.0/24",
    ]
    assert [h.gateway for h in r.chain] == ["198.51.100.2", "192.0.2.9", "10.1.1.1"]
    assert r.final_gateway == "10.1.1.1"
    assert r.interface == "eth0"
    assert r.connected_prefix == "10.0.0.0/8"
    # the intermediate routes carry their own shorter chains
    assert chain_prefixes(by_prefix["198.51.100.0/24"]) == [
        "198.51.100.0/24",
        "192.0.2.0/24",
    ]
    assert chain_prefixes(by_prefix["192.0.2.0/24"]) == ["192.0.2.0/24"]


def test_lpm_order_prefers_longer_covering_route():
    # gateway 10.1.2.3 is covered by /8, /16 and /24; the /24 must be tried
    # first even when a shorter route would also succeed
    connected = [C("192.0.2.0/24", "eth0")]
    static = [
        S("10.0.0.0/8", "192.0.2.8"),       # short cover, also viable
        S("10.1.0.0/16", "192.0.2.9"),      # medium cover, viable
        S("10.1.2.0/24", "192.0.2.10"),     # longest cover, viable
        S("172.16.0.0/12", "10.1.2.3"),
    ]
    resolved, unresolved = resolve_routes(connected, static)
    (r,) = [x for x in resolved if x.prefix == "172.16.0.0/12"]
    assert chain_prefixes(r) == ["172.16.0.0/12", "10.1.2.0/24"]
    assert r.final_gateway == "192.0.2.10"


def test_mutual_loop_falls_back_to_shorter_cover():
    connected = [C("10.0.0.0/8", "eth0")]
    static = [
        S("172.16.1.0/24", "172.16.2.1"),   # A -> B
        S("172.16.2.0/24", "172.16.1.1"),   # B -> A (loop)
        S("172.16.0.0/16", "10.9.9.9"),     # C covers both, reaches connected
    ]
    resolved, unresolved = resolve_routes(connected, static)
    assert unresolved == []
    by_prefix = {r.prefix: r for r in resolved}
    # A walks A -> B, the B -> A edge repeats a route and is pruned, then C
    assert chain_prefixes(by_prefix["172.16.1.0/24"]) == [
        "172.16.1.0/24",
        "172.16.2.0/24",
        "172.16.0.0/16",
    ]
    assert chain_prefixes(by_prefix["172.16.2.0/24"]) == [
        "172.16.2.0/24",
        "172.16.0.0/16",
    ]
    for r in resolved:
        assert r.final_gateway == "10.9.9.9"
        assert r.interface == "eth0"


def test_pure_loop_is_unresolved_loop():
    static = [
        S("172.16.1.0/24", "172.16.2.1"),
        S("172.16.2.0/24", "172.16.1.1"),
    ]
    _resolved, unresolved = resolve_routes([C("10.0.0.0/8", "eth0")], static)
    assert len(unresolved) == 2
    assert {u.prefix for u in unresolved} == {"172.16.1.0/24", "172.16.2.0/24"}
    assert all(u.reason == LOOP for u in unresolved)
    assert all(u.gateway for u in unresolved)


def test_self_loop_reported_as_loop():
    static = [S("172.20.0.0/16", "172.20.5.5")]
    _r, unresolved = resolve_routes([C("10.0.0.0/8", "eth0")], static)
    (u,) = unresolved
    assert u.prefix == "172.20.0.0/16"
    assert u.reason == LOOP


def test_node_leading_only_into_loop_is_loop():
    # X's gateway is covered by X/16 itself and by the looping /24 pair;
    # the /24 is longer so it is tried first and loops, the self edge loops
    # too. X can never leave: loop, not no_route.
    connected = [C("10.0.0.0/8", "eth0")]
    static = [
        S("172.16.0.0/16", "172.16.1.9"),
        S("172.16.1.0/24", "172.16.2.1"),
        S("172.16.2.0/24", "172.16.1.1"),
    ]
    _r, unresolved = resolve_routes(connected, static)
    reasons = {u.prefix: u.reason for u in unresolved}
    assert reasons == {
        "172.16.0.0/16": LOOP,
        "172.16.1.0/24": LOOP,
        "172.16.2.0/24": LOOP,
    }


def test_gateway_without_any_match_is_no_route():
    static = [S("172.19.0.0/16", "203.0.113.7")]
    _r, unresolved = resolve_routes([C("10.0.0.0/8", "eth0")], static)
    (u,) = unresolved
    assert u.reason == NO_ROUTE
    assert u.gateway == "203.0.113.7"


def test_chain_leading_to_dead_end_is_no_route():
    connected = [C("10.0.0.0/8", "eth0")]
    static = [
        S("172.16.0.0/16", "172.17.0.1"),    # points at the dead-end route
        S("172.17.0.0/16", "203.0.113.7"),   # gateway matches nothing
    ]
    _r, unresolved = resolve_routes(connected, static)
    reasons = {u.prefix: u.reason for u in unresolved}
    assert reasons == {"172.16.0.0/16": NO_ROUTE, "172.17.0.0/16": NO_ROUTE}


def test_loop_branch_skipped_when_another_branch_escapes():
    # the longest cover is a self-loop; it is pruned and a shorter cover
    # reaches the connected prefix instead
    connected = [C("192.0.2.0/24", "eth0")]
    static = [
        S("10.1.2.0/24", "10.1.2.9"),         # self-loop candidate (longest)
        S("10.1.0.0/16", "192.0.2.9"),        # escape via shorter cover
        S("172.16.0.0/12", "10.1.2.3"),
    ]
    resolved, unresolved = resolve_routes(connected, static)
    by_prefix = {r.prefix: r for r in resolved}
    assert unresolved == []
    # 172.16/12 first tries the /24 (self edge repeats the route -> pruned),
    # then falls back to the /16 which terminates on-link
    assert chain_prefixes(by_prefix["172.16.0.0/12"]) == [
        "172.16.0.0/12",
        "10.1.2.0/24",
        "10.1.0.0/16",
    ]
    # the looping /24 itself has the same escape through the /16
    assert chain_prefixes(by_prefix["10.1.2.0/24"]) == [
        "10.1.2.0/24",
        "10.1.0.0/16",
    ]


def test_output_order_matches_input_order():
    connected = [C("192.0.2.0/24", "eth0")]
    static = [
        S("203.0.113.5/32", "192.0.2.1"),
        S("203.0.113.0/24", "192.0.2.2"),
        S("198.51.100.0/24", "198.18.0.1"),   # dead end, stays in place
        S("10.0.0.0/8", "192.0.2.3"),
    ]
    resolved, unresolved = resolve_routes(connected, static)
    assert [r.prefix for r in resolved] == [
        "203.0.113.5/32",
        "203.0.113.0/24",
        "10.0.0.0/8",
    ]
    assert [u.prefix for u in unresolved] == ["198.51.100.0/24"]


def test_full_input_with_mixed_outcomes():
    connected = [
        C("192.168.10.0/24", "eth1"),
        C("192.168.10.128/25", "eth2"),
        C("10.0.0.0/8", "eth0"),
    ]
    static = [
        S("172.16.1.0/24", "172.16.2.1"),
        S("172.16.2.0/24", "172.16.1.1"),
        S("172.16.0.0/16", "10.9.9.9"),
        S("172.17.0.0/16", "172.16.1.254"),
        S("172.18.0.0/16", "192.168.10.200"),
        S("172.19.0.0/16", "203.0.113.7"),
        S("172.20.0.0/16", "172.20.5.5"),
    ]
    resolved, unresolved = resolve_routes(connected, static)
    assert len(resolved) == 5 and len(unresolved) == 2
    assert {u.reason for u in unresolved} == {LOOP, NO_ROUTE}
    # every resolved chain is internally consistent
    for r in resolved:
        assert r.chain[0].prefix == r.prefix
        for hop in r.chain:
            assert IPv4Address(hop.gateway)  # well-formed
        assert r.nexthop_id == f"{r.interface}:{r.final_gateway}"
        assert r.compress_route == {"prefix": r.prefix, "action": r.nexthop_id}


# ---------------------------------------------------------------------------
# brute-force oracle
# ---------------------------------------------------------------------------

def _brute_force(connected, static):
    """Independent reference: plain DFS over candidates with path pruning."""
    nets = [n for n, _ in static]
    gw_of = {n: g for n, g in static}

    def covers(net, addr):
        return (
            int(net.network_address) <= int(addr) <= int(net.broadcast_address)
        )

    def candidates(addr):
        term = sorted(
            (n for n, _ in connected if covers(n, addr)),
            key=lambda n: n.prefixlen,
            reverse=True,
        )
        if term:
            return [("T", term[0])]
        stat = sorted(
            (n for n in nets if covers(n, addr)),
            key=lambda n: n.prefixlen,
            reverse=True,
        )
        return [("S", n) for n in stat]

    memo = {}

    def walk(net, on_path):
        key = (net, frozenset(on_path))
        if key in memo:
            return memo[key]
        for kind, cand in candidates(gw_of[net]):
            if kind == "T":
                memo[key] = [net]
                return memo[key]
            if cand in on_path:
                continue
            sub = walk(cand, on_path | {net})
            if sub is not None:
                memo[key] = [net] + sub
                return memo[key]
        memo[key] = None
        return None

    def loop_bound(net):
        """Reference loop test: does some candidate walk revisit a route?

        A non-good node is loop-bound iff one of its walks closes a cycle
        (it has no terminal path by construction of the caller).
        """
        stack = [(net, frozenset())]
        while stack:
            cur, on_path = stack.pop()
            for kind, cand in candidates(gw_of[cur]):
                if kind == "T":
                    continue
                if cand in on_path | {cur}:
                    return True
                stack.append((cand, on_path | {cur}))
        return False

    out_ok, out_bad = [], {}
    for net, _ in static:
        chain = walk(net, frozenset())
        if chain is None:
            out_bad[str(net)] = LOOP if loop_bound(net) else NO_ROUTE
        else:
            out_ok.append((str(net), [str(n) for n in chain]))
    return out_ok, out_bad


def _random_inputs(rng):
    # a handful of connected prefixes under 10.0.0.0/8
    connected = []
    for _ in range(rng.randint(1, 3)):
        plen = rng.choice([16, 20, 24, 25, 28])
        net = IPv4Network((rng.randrange(10 << 24, (11 << 24)), plen), strict=False)
        connected.append(net)
    connected = list(dict.fromkeys(connected))
    if not connected:
        connected = [IPv4Network("10.0.0.0/8")]

    static = []
    seen = set()
    for _ in range(rng.randint(0, 9)):
        plen = rng.choice([8, 12, 16, 20, 24, 25, 32])
        net = IPv4Network((rng.getrandbits(32), plen), strict=False)
        if str(net) in seen:
            continue
        seen.add(str(net))
        kind = rng.randrange(4)
        if kind == 0 and connected:  # gateway on-link
            base = connected[rng.randrange(len(connected))]
            gw = IPv4Address(
                int(base.network_address)
                + rng.randrange(1, max(2, base.num_addresses - 1))
            )
        elif kind == 1 and static:  # gateway inside an existing static prefix
            base = static[rng.randrange(len(static))][0]
            gw = IPv4Address(
                int(base.network_address)
                + rng.randrange(0, base.num_addresses)
            )
        else:  # arbitrary gateway, often a dead end
            gw = IPv4Address(rng.getrandbits(32))
        static.append((net, gw))

    conn = [(n, f"if{i}") for i, n in enumerate(connected)]
    return conn, static


def test_random_matches_brute_force():
    rng = random.Random(20261120)
    for _ in range(400):
        connected, static = _random_inputs(rng)
        resolved, unresolved = resolve_routes(connected, static)
        bf_ok, bf_bad = _brute_force(connected, static)

        assert {u.prefix: u.reason for u in unresolved} == bf_bad
        got = {r.prefix: chain_prefixes(r) for r in resolved}
        assert set(got) == {p for p, _ in bf_ok}
        for prefix, bf_chain in bf_ok:
            assert got[prefix] == bf_chain

        # structural invariants on every reported chain
        for r in resolved:
            assert r.chain[0].prefix == r.prefix
            prefixes = chain_prefixes(r)
            assert len(prefixes) == len(set(prefixes))  # no repeated route
            # each hop's gateway must be covered by the next hop's prefix,
            # or be on-link at the final step
            for hop, next_prefix in zip(r.chain, prefixes[1:]):
                nxt = IPv4Network(next_prefix)
                assert (
                    int(nxt.network_address)
                    <= int(IPv4Address(hop.gateway))
                    <= int(nxt.broadcast_address)
                )
            final = IPv4Address(r.final_gateway)
            conn = IPv4Network(r.connected_prefix)
            assert int(conn.network_address) <= int(final) <= int(conn.broadcast_address)


# ---------------------------------------------------------------------------
# stress: no chain enumeration, full 128-route inputs
# ---------------------------------------------------------------------------

def test_stress_dense_dag_128_routes():
    # up to 128 distinct prefixes nested inside one /12 block: every gateway
    # can have dozens of covering candidates, giving exponentially many
    # simple paths; memoized DFS must stay polynomial
    rng = random.Random(1)
    connected = [C("10.0.0.0/8", "eth0")]
    static = []
    seen = set()
    while len(static) < 127:
        plen = rng.choice([13, 14, 15, 16, 17, 18, 20, 22, 24, 26, 28, 30])
        net = IPv4Network((rng.randrange(0xAC100000, 0xAC200000), plen), strict=False)
        if str(net) in seen:
            continue
        seen.add(str(net))
        gateway = IPv4Address(rng.randrange(0xAC100000, 0xAC200000))
        static.append((net, gateway))
    static.append(S("198.51.100.0/24", "10.1.1.1"))  # one terminal anchor

    start = time.monotonic()
    resolved, unresolved = resolve_routes(connected, static)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"resolution too slow: {elapsed:.3f}s"
    assert len(resolved) + len(unresolved) == 128
    for r in resolved:
        assert len(set(chain_prefixes(r))) == len(chain_prefixes(r))


def test_stress_128_node_loop_with_single_exit():
    # a 127-node ring of disjoint /16s where the last node breaks out to an
    # on-link gateway. Every ring node must resolve without chain enumeration.
    connected = [C("10.0.0.0/8", "eth0")]
    static = []
    base = 0xAC100000
    n_ring = 127
    for i in range(n_ring):
        net = IPv4Network((base + (i << 16), 16), strict=True)
        if i == n_ring - 1:
            gateway = IPv4Address("10.1.1.1")  # on-link exit
        else:
            j = i + 1
            gateway = IPv4Network((base + (j << 16), 16)).network_address + 1
        static.append((net, IPv4Address(gateway)))

    start = time.monotonic()
    resolved, unresolved = resolve_routes(connected, static)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"resolution too slow: {elapsed:.3f}s"
    assert unresolved == []
    assert len(resolved) == n_ring
    for r in resolved:
        assert r.final_gateway == "10.1.1.1"
        assert r.interface == "eth0"
    # the first node walks the entire ring up to the exit
    first = next(r for r in resolved if r.prefix == str(static[0][0]))
    assert len(chain_prefixes(first)) == n_ring


def test_stress_pure_cycle_128_nodes():
    # a single 128-node ring with no exit: all marked loop, no enumeration
    static = []
    base = 0xAC100000
    for i in range(128):
        net = IPv4Network((base + (i << 16), 16), strict=True)
        j = (i + 1) % 128
        gateway = IPv4Network((base + (j << 16), 16)).network_address + 1
        static.append((net, IPv4Address(gateway)))
    start = time.monotonic()
    resolved, unresolved = resolve_routes([C("10.0.0.0/8", "eth0")], static)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"resolution too slow: {elapsed:.3f}s"
    assert resolved == []
    assert len(unresolved) == 128
    assert all(u.reason == LOOP for u in unresolved)


def test_nexthop_id_within_compress_action_limit():
    # 32-char interface + IPv4 address still fits /compress action (max 64)
    iface = "x" * 32
    resolved, _ = resolve_routes(
        [C("192.0.2.0/24", iface)],
        [S("203.0.113.0/24", "192.0.2.2")],
    )
    assert len(resolved[0].nexthop_id) <= 64
