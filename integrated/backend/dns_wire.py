"""Bounded DNS record decoding shared by both editions."""
import socket
import struct

RECORD_TYPES = {"A": 1, "NS": 2, "CNAME": 5, "PTR": 12, "MX": 15,
                "TXT": 16, "AAAA": 28, "SRV": 33, "SVCB": 64, "HTTPS": 65}
TYPE_NAMES = {value: key for key, value in RECORD_TYPES.items()}
RCODE_NAMES = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN",
               4: "NOTIMP", 5: "REFUSED", 16: "BADVERS"}


def parse_response(packet, query_id, domain, record_type, read_name):
    if len(packet) < 12:
        raise ValueError("DNS response too short")
    response_id, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", packet[:12])
    if response_id != query_id:
        raise ValueError("DNS response ID mismatch")
    if not flags & 0x8000 or flags & 0x7800 or qd != 1:
        raise ValueError("Not a standard single-question DNS response")
    name, offset = read_name(packet, 12)
    if offset + 4 > len(packet):
        raise ValueError("DNS question exceeds packet length")
    qtype, qclass = struct.unpack_from("!HH", packet, offset)
    if domain is not None and name.lower().rstrip(".") != domain.lower().rstrip("."):
        raise ValueError("DNS response question mismatch")
    if qclass != 1 or (record_type and qtype != RECORD_TYPES[record_type]):
        raise ValueError("DNS response question type/class mismatch")
    offset += 4
    sections = {"answers": [], "authority": [], "additional": []}
    extended_rcode = 0
    saw_opt = False
    if not flags & 0x0200:
        if an + ns + ar > 512:
            raise ValueError("DNS response has too many records")
        for section, count in zip(sections, (an, ns, ar)):
            for _ in range(count):
                owner, offset = read_name(packet, offset)
                if offset + 10 > len(packet):
                    raise ValueError("DNS record exceeds packet length")
                kind, cls, ttl, length = struct.unpack_from("!HHIH", packet, offset)
                start = offset + 10
                end = start + length
                if end > len(packet):
                    raise ValueError("DNS record data exceeds packet length")
                data = packet[start:end]

                def name_at(position, exact=True):
                    if position >= end:
                        raise ValueError("Missing DNS record name")
                    value, following = read_name(packet, position)
                    if following > end or (exact and following != end):
                        raise ValueError("Invalid DNS name record length")
                    return value or ".", following

                value = data.hex()
                if kind in (1, 28):
                    if length != (4 if kind == 1 else 16):
                        raise ValueError("Invalid DNS address record length")
                    value = socket.inet_ntop(socket.AF_INET if kind == 1 else socket.AF_INET6, data)
                elif kind in (2, 5, 12):
                    value, _ = name_at(start)
                elif kind in (15, 33):
                    prefix = 2 if kind == 15 else 6
                    if length < prefix + 1:
                        raise ValueError("Invalid MX/SRV record length")
                    numbers = struct.unpack("!H" if kind == 15 else "!HHH", data[:prefix])
                    target, _ = name_at(start + prefix)
                    value = " ".join([*(str(n) for n in numbers), target])
                elif kind == 16:
                    strings = []
                    cursor = 0
                    while cursor < length:
                        size = data[cursor]
                        cursor += 1
                        if cursor + size > length:
                            raise ValueError("Invalid TXT string length")
                        strings.append(data[cursor:cursor + size].decode("utf-8", errors="replace"))
                        cursor += size
                    value = "".join(strings)
                elif kind in (64, 65):
                    if length < 3:
                        raise ValueError("Invalid SVCB/HTTPS record length")
                    priority = struct.unpack_from("!H", data)[0]
                    target, cursor = name_at(start + 2, exact=False)
                    params = []
                    previous = -1
                    while cursor < end:
                        if cursor + 4 > end:
                            raise ValueError("Invalid SVCB parameter")
                        key, size = struct.unpack_from("!HH", packet, cursor)
                        cursor += 4
                        if key <= previous or cursor + size > end:
                            raise ValueError("Invalid SVCB parameter order/length")
                        params.append(f"key{key}=0x{packet[cursor:cursor + size].hex()}")
                        previous = key
                        cursor += size
                    value = " ".join([str(priority), target, *params])
                elif kind == 41:
                    if section != "additional" or owner or saw_opt:
                        raise ValueError("Invalid EDNS record")
                    saw_opt = True
                    extended_rcode = ttl >> 24
                sections[section].append({"name": owner, "type": TYPE_NAMES.get(kind, f"TYPE{kind}"),
                                          "ttl": ttl, "value": value, "class": cls})
                offset = end
    rcode = (extended_rcode << 4) | (flags & 15)
    debug = [record for record in sections["additional"] if record["type"] == "TXT"
             and record["class"] == 1 and record["name"].lower().endswith(
                 (".addinfo.paopaodns", ".reject.paopaodns", ".host.paopaodns",
                  ".swap.paopaodns", ".refused.paopaodns"))]
    return {**sections, "debug": debug, "rcode": RCODE_NAMES.get(rcode, f"RCODE{rcode}"),
            "rcode_value": rcode, "truncated": bool(flags & 0x0200),
            "authoritative": bool(flags & 0x0400), "recursion_available": bool(flags & 0x0080),
            "counts": {"questions": qd, "answers": an, "authority": ns, "additional": ar}}
