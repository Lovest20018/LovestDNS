import struct

import pytest
import server
from dns_wire import parse_response


def packet(kind, data, section="answers", flags=0x8180):
    query = (struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0)
             + server.base._encode_dns_name("example.com")
             + struct.pack("!HH", server.base.DNS_RECORD_TYPES[kind], 1))
    counts = [1 if key == section else 0 for key in ("answers", "authority", "additional")]
    record = b"\xc0\x0c" + struct.pack("!HHIH", server.base.DNS_RECORD_TYPES[kind], 1, 0, len(data)) + data
    return struct.pack("!HHHHHH", 7, flags, 1, *counts) + query[12:] + record


@pytest.mark.parametrize("kind,data,expected", [
    ("TXT", b"\x03one\x03two", "onetwo"),
    ("NS", b"\xc0\x0c", "example.com"),
    ("PTR", b"\xc0\x0c", "example.com"),
    ("MX", b"\x00\x0a\xc0\x0c", "10 example.com"),
    ("SRV", b"\x00\x01\x00\x02\x01\xbb\xc0\x0c", "1 2 443 example.com"),
    ("HTTPS", b"\x00\x01\x00\x00\x03\x00\x02\x01\xbb", "1 . key3=0x01bb"),
])
def test_supported_types_and_compression(kind, data, expected):
    result = parse_response(packet(kind, data), 7, "example.com", kind, server.base._read_dns_name)
    assert result["answers"][0]["value"] == expected
    assert result["answers"][0]["ttl"] == 0


@pytest.mark.parametrize("kind,data", [("TXT", b"\x05x"), ("MX", b"\x00"), ("SRV", b"\x00" * 5),
                                     ("HTTPS", b"\x00\x01\x00\x00\x03\x00\x05\x01")])
def test_malformed_rdata_is_rejected(kind, data):
    with pytest.raises(ValueError):
        parse_response(packet(kind, data), 7, "example.com", kind, server.base._read_dns_name)


def test_authority_and_additional_are_not_target_answers():
    result = parse_response(packet("TXT", b"\x03abc", "additional"), 7, "example.com", "TXT", server.base._read_dns_name)
    assert result["answers"] == []
    assert result["additional"][0]["value"] == "abc"
    assert result["debug"] == []
