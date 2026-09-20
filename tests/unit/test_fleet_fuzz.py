"""Protocol fuzz / injection resistance for fleet envelopes."""

from __future__ import annotations

import pytest

from backuplint.fleet.protocol import PROTOCOL_VERSION, ProtocolError, parse_envelope


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "x",
        {},
        {"protocol_version": PROTOCOL_VERSION},
        {
            "protocol_version": PROTOCOL_VERSION,
            "agent_id": "../etc/passwd",
            "submission_id": "s",
            "scan_time": "t",
            "backuplint_version": "v",
            "platform": "p",
            "result": {},
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "agent_id": "agent-abcdefgh",
            "submission_id": "s",
            "scan_time": "t",
            "backuplint_version": "v",
            "platform": "p",
            "result": {},
            "extra": 1,
        },
        {
            "protocol_version": PROTOCOL_VERSION,
            "agent_id": "agent-abcdefgh",
            "submission_id": "s",
            "scan_time": "t",
            "backuplint_version": "v",
            "platform": "p",
            "result": {"token": "leak"},
        },
        {
            "protocol_version": "1",
            "agent_id": "agent-abcdefgh",
            "submission_id": "s",
            "scan_time": "t",
            "backuplint_version": "v",
            "platform": "p",
            "result": {},
        },
    ],
)
def test_parse_envelope_rejects_garbage(payload: object) -> None:
    with pytest.raises(ProtocolError):
        parse_envelope(payload)
