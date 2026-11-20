"""FIB compressor service.

Edge-router hardware migration helper: squeeze the current IPv4 forwarding
table into a new device's entry budget.  Given up to 128 routes with
distinct prefixes, a device capacity, and a next-hop to drill-fail, the
service returns the smallest equivalent table (new prefixes may be used,
actions are reused from the original table).  Equivalence means identical
longest-prefix-match results for every destination address — with "drop"
and "no-match" kept distinct — both before and after withdrawing the
failed next-hop from old and new tables.  When the minimum does not fit
the capacity, only the minimum required entry count is reported.

POST /resolve additionally turns recursive static routes (IPv4 next-hops
resolved hop by hop through the static table until a directly connected
prefix is reached, with loop branches pruned) into routes that can be
submitted to /compress unchanged.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .compressor import DROP, compress, verify
from .resolver import LOOP, NO_ROUTE, resolve_routes

APP_VERSION = "1.1.0"
MAX_ROUTES = 128
MAX_INTERFACE_LEN = 32

# ---------------------------------------------------------------------------
# demo payloads (shown in /docs)
# ---------------------------------------------------------------------------

DEMO_ROUTES = [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"},
]

EXAMPLE_CAPACITY_OK = {
    "summary": "容量足够：5 条压到 4 条，故障切换保持等价",
    "description": (
        "10.2.0.0/16 与默认的 10.0.0.0/8 同为 NH1，被合并吸收；"
        "撤掉 NH2 后 10.1.0.0/16 回落到 NH1、192.168.0.0/16 变为未匹配，"
        "新旧表两种场景下逐地址一致。最少需要 4 条，容量 4 刚好装下。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 4,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_CAPACITY_EXCEEDED = {
    "summary": "容量不够：只回报最低所需条数",
    "description": (
        "同一张表演练失效 NH2，但新设备只能装 3 条。等效表最少需要 4 条，"
        "超过容量，因此只返回 min_entries=4，不下发转发表。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 3,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_FAILOVER_LADDER = {
    "summary": "故障回落链：被撤下一跳后回落到更短前缀",
    "description": (
        "10.0.0.0/8→NH1、10.1.0.0/16→NH2、10.1.1.0/24→NH3，演练失效 NH2。"
        "撤掉 NH2 后 10.1.0.0/16 内（除 10.1.1.0/24）须回落到 NH1。"
        "压缩结果保持正常与故障两种场景逐地址等价。"
    ),
    "value": {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1"},
            {"prefix": "10.1.0.0/16", "action": "NH2"},
            {"prefix": "10.1.1.0/24", "action": "NH3"},
        ],
        "capacity": 8,
        "failed_nexthop": "NH2",
    },
}

# ---------------------------------------------------------------------------
# /resolve demo payloads (shown in /docs)
# ---------------------------------------------------------------------------

RESOLVE_DEMO_CONNECTED = [
    {"prefix": "192.168.10.0/24", "interface": "eth1"},
    {"prefix": "192.168.10.128/25", "interface": "eth2"},
    {"prefix": "10.0.0.0/8", "interface": "eth0"},
]

RESOLVE_DEMO_STATIC = [
    # A <-> B form a loop; C's /8 covers both gateways and escapes to eth0,
    # so both fall back to C instead of enumerating chains.
    {"prefix": "172.16.1.0/24", "next_hop": "172.16.2.1"},
    {"prefix": "172.16.2.0/24", "next_hop": "172.16.1.1"},
    {"prefix": "172.16.0.0/16", "next_hop": "10.9.9.9"},
    # single-hop recursion to a connected gateway
    {"prefix": "172.17.0.0/16", "next_hop": "172.16.1.254"},
    # connected longest match: 192.168.10.200 hits the /25 -> eth2, not eth1
    {"prefix": "172.18.0.0/16", "next_hop": "192.168.10.200"},
    # dead end: gateway matches no connected prefix and no static route
    {"prefix": "172.19.0.0/16", "next_hop": "203.0.113.7"},
    # self-loop: the only covering route is the route itself
    {"prefix": "172.20.0.0/16", "next_hop": "172.20.5.5"},
]

EXAMPLE_RESOLVE_LOOP = {
    "summary": "遇环回退：A、B 互相指向，经更短的 C/16 回退到直连网段",
    "description": (
        "172.16.1.0/24 与 172.16.2.0/24 互为下一跳构成环路；解析时跳过会重复"
        "经过同一路由的分支，改用覆盖两者的 172.16.0.0/16（下一跳 10.9.9.9 在"
        "直连 10.0.0.0/8 内）完成依赖链。172.20.0.0/16 自环、172.19.0.0/16 "
        "无路可走，均列入 unresolved；全量输入仍正常处理，不穷举任何链。"
    ),
    "value": {
        "connected": RESOLVE_DEMO_CONNECTED,
        "static_routes": RESOLVE_DEMO_STATIC,
    },
}

EXAMPLE_RESOLVE_STRAIGHT = {
    "summary": "直链解析：下一跳两次递归后落到直连网段",
    "description": (
        "203.0.113.0/24→198.51.100.2→192.0.2.2，后者在直连 192.0.2.0/24（eth0）"
        "内。返回依赖链、出接口 eth0、最终网关 192.0.2.2，并生成可直接提交给"
        "/compress 的 routes。"
    ),
    "value": {
        "connected": [
            {"prefix": "192.0.2.0/24", "interface": "eth0"},
        ],
        "static_routes": [
            {"prefix": "203.0.113.0/24", "next_hop": "198.51.100.2"},
            {"prefix": "198.51.100.0/24", "next_hop": "192.0.2.2"},
        ],
    },
}


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

class Route(BaseModel):
    """One forwarding entry: a CIDR prefix and its action.

    ``action`` is a next-hop name, or the reserved word ``drop`` to discard
    matching traffic.  Addresses that match no entry are "no-match", which
    is distinct from ``drop``.
    """

    prefix: str = Field(examples=["10.0.0.0/8"])
    action: str = Field(min_length=1, max_length=64, examples=["NH1"])

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        try:
            net = ip_network(v.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 CIDR prefix: {v!r}") from exc
        if not isinstance(net, IPv4Network):
            raise ValueError(f"not an IPv4 prefix: {v!r}")
        return str(net)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        a = v.strip()
        if not a:
            raise ValueError("action must be a non-empty next-hop name or 'drop'")
        return a


class CompressRequest(BaseModel):
    routes: List[Route] = Field(
        max_length=MAX_ROUTES,
        description=f"Up to {MAX_ROUTES} routes with distinct prefixes.",
    )
    capacity: int = Field(ge=0, description="Entry budget of the new device.")
    failed_nexthop: str = Field(
        min_length=1,
        max_length=64,
        description="Next-hop withdrawn in the failure drill.",
    )

    @field_validator("failed_nexthop")
    @classmethod
    def _check_failed(cls, v: str) -> str:
        f = v.strip()
        if not f:
            raise ValueError("failed_nexthop must be non-empty")
        return f

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        prefixes = [r.prefix for r in self.routes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("route prefixes must be distinct")
        return self


class CompressResponse(BaseModel):
    status: str = Field(description="'ok' or 'capacity_exceeded'.")
    fits: bool = Field(description="Whether the minimal table fits the capacity.")
    min_entries: int = Field(description="Minimal number of entries of any equivalent table.")
    capacity: int
    failed_nexthop: str
    table: Optional[List[Route]] = Field(
        description="The minimal equivalent table; null when over capacity."
    )
    verified: Optional[bool] = Field(
        description=(
            "Server-side re-check that old and new tables agree on every "
            "address in both the normal and the failure scenario."
        )
    )


class VerifyRequest(BaseModel):
    old_routes: List[Route] = Field(max_length=4096)
    new_routes: List[Route] = Field(max_length=4096)
    failed_nexthop: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        for name, rs in (("old_routes", self.old_routes), ("new_routes", self.new_routes)):
            prefixes = [r.prefix for r in rs]
            if len(set(prefixes)) != len(prefixes):
                raise ValueError(f"{name}: prefixes must be distinct")
        return self


class VerifyResponse(BaseModel):
    equivalent: bool
    normal_equivalent: bool
    failover_equivalent: bool
    checked_points: int = Field(
        description="Number of elementary address intervals compared."
    )
    first_mismatch: Optional[dict] = None


# ---------------------------------------------------------------------------
# /resolve schemas
# ---------------------------------------------------------------------------

class ConnectedRoute(BaseModel):
    """A directly attached IPv4 prefix and the egress interface it sits on."""

    prefix: str = Field(examples=["192.168.10.0/24"])
    interface: str = Field(
        min_length=1,
        max_length=MAX_INTERFACE_LEN,
        examples=["eth1"],
        description="Egress interface name (at most 32 characters).",
    )

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        try:
            net = ip_network(v.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 CIDR prefix: {v!r}") from exc
        if not isinstance(net, IPv4Network):
            raise ValueError(f"not an IPv4 prefix: {v!r}")
        return str(net)

    @field_validator("interface")
    @classmethod
    def _check_interface(cls, v: str) -> str:
        name = v.strip()
        if not name:
            raise ValueError("interface must be a non-empty name")
        if len(name) > MAX_INTERFACE_LEN:
            raise ValueError(
                f"interface name too long (max {MAX_INTERFACE_LEN} characters)"
            )
        return name


class StaticRouteInput(BaseModel):
    """A static route: destination prefix and an IPv4 next-hop address."""

    prefix: str = Field(examples=["172.16.1.0/24"])
    next_hop: str = Field(examples=["172.16.2.1"])

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        try:
            net = ip_network(v.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 CIDR prefix: {v!r}") from exc
        if not isinstance(net, IPv4Network):
            raise ValueError(f"not an IPv4 prefix: {v!r}")
        return str(net)

    @field_validator("next_hop")
    @classmethod
    def _check_next_hop(cls, v: str) -> str:
        try:
            addr = ip_address(v.strip())
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 next-hop address: {v!r}") from exc
        if not isinstance(addr, IPv4Address):
            raise ValueError(f"not an IPv4 next-hop address: {v!r}")
        return str(addr)


class ResolveRequest(BaseModel):
    connected: List[ConnectedRoute] = Field(
        min_length=1,
        max_length=MAX_ROUTES,
        description="Directly attached IPv4 prefixes with egress interfaces; "
        "prefixes are mutually distinct.",
    )
    static_routes: List[StaticRouteInput] = Field(
        max_length=MAX_ROUTES,
        description="Up to 128 static routes whose next-hop is an IPv4 "
        "address; prefixes are mutually distinct.",
    )

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        for name, items in (
            ("connected", self.connected),
            ("static_routes", self.static_routes),
        ):
            prefixes = [r.prefix for r in items]
            if len(set(prefixes)) != len(prefixes):
                raise ValueError(f"{name}: prefixes must be distinct")
        return self


class ResolveHop(BaseModel):
    prefix: str = Field(description="Route prefix used at this chain step.")
    next_hop: str = Field(description="That route's next-hop gateway address.")


class CompressRoute(BaseModel):
    prefix: str
    action: str = Field(
        description="Next-hop identifier 'interface:final_gateway'."
    )


class ResolvedRouteOut(BaseModel):
    prefix: str = Field(description="The static route's own destination prefix.")
    chain: List[ResolveHop] = Field(
        description="Dependency chain starting at the route's own prefix; "
        "each hop's gateway is resolved by the next hop."
    )
    interface: str = Field(description="Egress interface of the final gateway.")
    final_gateway: str = Field(
        description="First on-link gateway, i.e. where recursion terminates."
    )
    connected_prefix: str = Field(
        description="Longest-matching connected prefix of the final gateway."
    )
    nexthop_id: str = Field(
        description="Next-hop identifier 'interface:final_gateway'."
    )
    route: CompressRoute = Field(
        description="Entry ready to submit inside POST /compress routes."
    )


class UnresolvedRouteOut(BaseModel):
    prefix: str
    next_hop: str
    reason: str = Field(
        description=f"'{LOOP}' (a cycle can never reach a connected prefix) "
        f"or '{NO_ROUTE}' (a gateway matches neither a connected nor a "
        "static route)."
    )


class ResolveResponse(BaseModel):
    status: str = Field(
        description="'ok' when every route resolved, 'partial' when some did not."
    )
    resolved_count: int
    unresolved_count: int
    resolved: List[ResolvedRouteOut]
    unresolved: List[UnresolvedRouteOut]
    routes: List[CompressRoute] = Field(
        description="All resolved routes, ready to submit to POST /compress."
    )


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FIB Compressor",
    version=APP_VERSION,
    description=(
        "Compress an IPv4 forwarding table for a smaller-capacity edge router. "
        "The compressed table is entry-minimal and equivalent under "
        "longest-prefix matching — distinguishing drop from no-match — both "
        "before and after a next-hop failure drill. "
        "POST /resolve first turns recursive static routes (IPv4 next-hops "
        "resolved through connected prefixes) into the /compress route format. "
        "Try the ready-made examples below, including a static-route loop that "
        "falls back to a shorter covering prefix."
    ),
)


@app.get("/", summary="Service info")
def root():
    return {
        "service": "fib-compressor",
        "version": APP_VERSION,
        "endpoints": {
            "POST /compress": "minimal equivalent table (or minimum count when over capacity)",
            "POST /verify": "check two tables for normal + failover equivalence",
            "POST /resolve": "recursive static-route resolution into /compress-ready routes",
            "GET /docs": "interactive demo",
        },
        "limits": {
            "max_routes": MAX_ROUTES,
            "max_interface_length": MAX_INTERFACE_LEN,
        },
        "reserved_action": DROP,
    }


@app.post("/compress", response_model=CompressResponse, summary="Compress a table")
def compress_endpoint(
    req: CompressRequest = Body(
        openapi_examples={
            "capacity_ok": EXAMPLE_CAPACITY_OK,
            "capacity_exceeded": EXAMPLE_CAPACITY_EXCEEDED,
            "failover_ladder": EXAMPLE_FAILOVER_LADDER,
        }
    )
):
    routes = [(IPv4Network(r.prefix), r.action) for r in req.routes]
    min_entries, table = compress(routes, req.failed_nexthop, req.capacity)

    if table is None:
        return CompressResponse(
            status="capacity_exceeded",
            fits=False,
            min_entries=min_entries,
            capacity=req.capacity,
            failed_nexthop=req.failed_nexthop,
            table=None,
            verified=None,
        )

    new_routes = [(IPv4Network(p), a) for p, a in table]
    ok, detail = verify(routes, new_routes, req.failed_nexthop)
    if not ok:  # pragma: no cover - indicates an algorithmic bug
        raise HTTPException(
            status_code=500,
            detail=f"internal verification failed: {detail['first_mismatch']}",
        )

    return CompressResponse(
        status="ok",
        fits=True,
        min_entries=min_entries,
        capacity=req.capacity,
        failed_nexthop=req.failed_nexthop,
        table=[Route(prefix=p, action=a) for p, a in table],
        verified=True,
    )


@app.post("/verify", response_model=VerifyResponse, summary="Check equivalence")
def verify_endpoint(req: VerifyRequest):
    old = [(IPv4Network(r.prefix), r.action) for r in req.old_routes]
    new = [(IPv4Network(r.prefix), r.action) for r in req.new_routes]
    ok, detail = verify(old, new, req.failed_nexthop)
    return VerifyResponse(equivalent=ok, **detail)


@app.post(
    "/resolve",
    response_model=ResolveResponse,
    summary="Resolve recursive static routes into /compress-ready entries",
)
def resolve_endpoint(
    req: ResolveRequest = Body(
        openapi_examples={
            "loop_fallback": EXAMPLE_RESOLVE_LOOP,
            "straight_chain": EXAMPLE_RESOLVE_STRAIGHT,
        }
    )
):
    connected = [
        (IPv4Network(r.prefix), r.interface) for r in req.connected
    ]
    static = [
        (IPv4Network(r.prefix), IPv4Address(r.next_hop))
        for r in req.static_routes
    ]
    resolved, unresolved = resolve_routes(connected, static)

    out_resolved = [
        ResolvedRouteOut(
            prefix=r.prefix,
            chain=[ResolveHop(prefix=h.prefix, next_hop=h.gateway) for h in r.chain],
            interface=r.interface,
            final_gateway=r.final_gateway,
            connected_prefix=r.connected_prefix,
            nexthop_id=r.nexthop_id,
            route=CompressRoute(**r.compress_route),
        )
        for r in resolved
    ]
    out_unresolved = [
        UnresolvedRouteOut(prefix=r.prefix, next_hop=r.gateway, reason=r.reason)
        for r in unresolved
    ]
    return ResolveResponse(
        status="ok" if not unresolved else "partial",
        resolved_count=len(out_resolved),
        unresolved_count=len(out_unresolved),
        resolved=out_resolved,
        unresolved=out_unresolved,
        routes=[CompressRoute(**r.compress_route) for r in resolved],
    )
