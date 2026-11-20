"""API tests for POST /resolve and its hand-off to POST /compress."""

from fastapi.testclient import TestClient

from app.main import (
    RESOLVE_DEMO_CONNECTED,
    RESOLVE_DEMO_STATIC,
    app,
)

client = TestClient(app)


def post_resolve(connected, static_routes):
    return client.post(
        "/resolve",
        json={"connected": connected, "static_routes": static_routes},
    )


def test_resolve_docs_example_loop_fallback():
    r = post_resolve(RESOLVE_DEMO_CONNECTED, RESOLVE_DEMO_STATIC)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "partial"
    assert body["resolved_count"] == 5
    assert body["unresolved_count"] == 2

    by_prefix = {x["prefix"]: x for x in body["resolved"]}

    # A <-> B loop, both escape via the covering /16
    a = by_prefix["172.16.1.0/24"]
    assert [h["prefix"] for h in a["chain"]] == [
        "172.16.1.0/24",
        "172.16.2.0/24",
        "172.16.0.0/16",
    ]
    assert a["interface"] == "eth0"
    assert a["final_gateway"] == "10.9.9.9"
    assert a["connected_prefix"] == "10.0.0.0/8"
    assert a["nexthop_id"] == "eth0:10.9.9.9"
    assert a["route"] == {
        "prefix": "172.16.1.0/24",
        "action": "eth0:10.9.9.9",
    }

    b = by_prefix["172.16.2.0/24"]
    assert [h["prefix"] for h in b["chain"]] == [
        "172.16.2.0/24",
        "172.16.0.0/16",
    ]

    # connected longest-prefix match: /25 beats /24
    e = by_prefix["172.18.0.0/16"]
    assert e["interface"] == "eth2"
    assert e["connected_prefix"] == "192.168.10.128/25"
    assert e["final_gateway"] == "192.168.10.200"

    reasons = {u["prefix"]: u["reason"] for u in body["unresolved"]}
    assert reasons == {
        "172.19.0.0/16": "no_route",
        "172.20.0.0/16": "loop",
    }

    # the hand-off table contains exactly the resolved routes
    assert body["routes"] == [x["route"] for x in body["resolved"]]
    assert len(body["routes"]) == 5


def test_resolve_straight_chain_status_ok():
    r = post_resolve(
        [{"prefix": "192.0.2.0/24", "interface": "eth0"}],
        [
            {"prefix": "203.0.113.0/24", "next_hop": "198.51.100.2"},
            {"prefix": "198.51.100.0/24", "next_hop": "192.0.2.2"},
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["unresolved"] == []
    first = body["resolved"][0]
    assert first["prefix"] == "203.0.113.0/24"
    assert [h["next_hop"] for h in first["chain"]] == [
        "198.51.100.2",
        "192.0.2.2",
    ]
    assert first["final_gateway"] == "192.0.2.2"


def test_resolve_then_compress_end_to_end():
    # 1) resolve recursive static routes
    r = post_resolve(RESOLVE_DEMO_CONNECTED, RESOLVE_DEMO_STATIC)
    routes = r.json()["routes"]

    # 2) submit the generated routes straight to /compress
    c = client.post(
        "/compress",
        json={
            "routes": routes,
            "capacity": 16,
            "failed_nexthop": "eth0:10.9.9.9",
        },
    )
    assert c.status_code == 200
    table = c.json()["table"]
    assert c.json()["verified"] is True
    # actions are reused from the resolver's next-hop identifiers
    assert {e["action"] for e in table} <= {e["action"] for e in routes}


def test_resolve_empty_static_routes():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "eth0"}], []
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["resolved"] == []
    assert body["unresolved"] == []
    assert body["routes"] == []


def test_resolve_prefix_normalization():
    r = post_resolve(
        [{"prefix": "10.1.2.3/8", "interface": "eth0"}],
        [{"prefix": "172.16.9.9/12", "next_hop": "10.5.5.5"}],
    )
    assert r.status_code == 200
    out = r.json()["resolved"][0]
    assert out["connected_prefix"] == "10.0.0.0/8"
    assert out["route"]["prefix"] == "172.16.0.0/12"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def test_resolve_validation_duplicate_connected_prefixes():
    r = post_resolve(
        [
            {"prefix": "10.0.0.0/8", "interface": "eth0"},
            {"prefix": "10.0.0.0/8", "interface": "eth1"},
        ],
        [],
    )
    assert r.status_code == 422


def test_resolve_validation_duplicate_static_prefixes():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "eth0"}],
        [
            {"prefix": "172.16.0.0/16", "next_hop": "10.0.0.1"},
            {"prefix": "172.16.0.0/16", "next_hop": "10.0.0.2"},
        ],
    )
    assert r.status_code == 422


def test_resolve_validation_interface_too_long():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "x" * 33}],
        [{"prefix": "172.16.0.0/16", "next_hop": "10.0.0.1"}],
    )
    assert r.status_code == 422


def test_resolve_validation_interface_blank():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "   "}],
        [],
    )
    assert r.status_code == 422


def test_resolve_validation_32_char_interface_accepted():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "x" * 32}],
        [{"prefix": "172.16.0.0/16", "next_hop": "10.0.0.1"}],
    )
    assert r.status_code == 200
    assert r.json()["resolved"][0]["interface"] == "x" * 32


def test_resolve_validation_bad_next_hop():
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "eth0"}],
        [{"prefix": "172.16.0.0/16", "next_hop": "not-an-ip"}],
    )
    assert r.status_code == 422


def test_resolve_validation_bad_connected_prefix():
    r = post_resolve(
        [{"prefix": "999.0.0.0/8", "interface": "eth0"}],
        [],
    )
    assert r.status_code == 422


def test_resolve_validation_connected_required():
    r = client.post(
        "/resolve",
        json={
            "connected": [],
            "static_routes": [
                {"prefix": "172.16.0.0/16", "next_hop": "10.0.0.1"}
            ],
        },
    )
    assert r.status_code == 422


def test_resolve_validation_too_many_static_routes():
    static = [
        {"prefix": f"172.{i}.0.0/16", "next_hop": "10.0.0.1"}
        for i in range(129)
    ]
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "eth0"}], static
    )
    assert r.status_code == 422


def test_resolve_exactly_128_static_routes_accepted():
    static = [
        {"prefix": f"172.{i}.0.0/16", "next_hop": "10.0.0.1"}
        for i in range(128)
    ]
    r = post_resolve(
        [{"prefix": "10.0.0.0/8", "interface": "eth0"}], static
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolved_count"] == 128
    assert len(body["routes"]) == 128


def test_resolve_listed_in_root_and_openapi():
    root = client.get("/").json()
    assert "POST /resolve" in root["endpoints"]
    assert root["limits"]["max_interface_length"] == 32
    spec = client.get("/openapi.json").json()
    assert "/resolve" in spec["paths"]
    examples = spec["paths"]["/resolve"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert set(examples) == {"loop_fallback", "straight_chain"}
