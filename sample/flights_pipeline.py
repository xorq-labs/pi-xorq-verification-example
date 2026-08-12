"""Sample xorq expression for trying out pi-xorq-verifier.

Builds a tiny ``flights-by-origin`` table (origin, n) so a data answer like
"ATL is the busiest origin with 17,875 flights" has a declared catalog alias to
be verified against. Inline ``memtable`` data keeps it offline and deterministic.

    xorq build sample/flights_pipeline.py --builds-dir .xorq/builds
    xorq catalog -p .xorq/catalog --init add <build-dir> -a flights-by-origin
"""

import xorq.api as xo


flights_by_origin = xo.memtable(
    [
        {"origin": "ATL", "n": 17875},
        {"origin": "ORD", "n": 12055},
        {"origin": "DFW", "n": 10985},
        {"origin": "DEN", "n": 9812},
    ],
    name="flights_by_origin",
)

expr = flights_by_origin.order_by(xo._.n.desc())
