"""Linux receive-only packet socket, filtered before any packets reach Python."""
import ctypes
import socket
import struct
import ipaddress


def local_addresses():
    """Read address inventory in this network namespace through rtnetlink."""
    result, total = set(), 0
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0) as sock:
        sock.settimeout(.5)
        sock.bind((0, 0))
        sock.send(struct.pack('IHHII', 24, 22, 0x301, 1, 0) + bytes(8))
        while True:
            data = sock.recv(65536)
            total += len(data)
            if total > 256 * 1024:
                raise ValueError('address_inventory_limit')
            offset = 0
            while offset + 16 <= len(data):
                length, kind, _, seq, _ = struct.unpack_from('IHHII', data, offset)
                if length < 16 or offset + length > len(data) or seq != 1:
                    raise ValueError('invalid_address_inventory')
                if kind == 3:
                    return result
                if kind == 2:
                    raise OSError('address_inventory_failed')
                if kind == 20 and length >= 24:
                    family = data[offset + 16]
                    attr = offset + 24
                    addresses = {}
                    while attr + 4 <= offset + length:
                        size, tag = struct.unpack_from('HH', data, attr)
                        if size < 4 or attr + size > offset + length:
                            raise ValueError('invalid_address_attribute')
                        raw = data[attr + 4:attr + size]
                        if tag in (1, 2) and (family, len(raw)) in {(2, 4), (10, 16)}:
                            addresses[tag] = str(ipaddress.ip_address(raw))
                        attr += (size + 3) & ~3
                    if addresses:
                        result.add(addresses.get(2, addresses.get(1)))
                offset += (length + 3) & ~3


def filter_program(ports):
    """Classic BPF on SOCK_DGRAM network headers; fragments/extensions omitted."""
    ports = sorted(set(ports))
    if not ports or len(ports) > 8 or any(type(p) is not int or not 0 < p < 65536 for p in ports):
        raise ValueError('unsupported_entry_ports')
    code, labels, jumps = [], {}, []

    def label(name):
        labels[name] = len(code)

    def ins(op, value=0):
        code.append([op, 0, 0, value])

    def jump(op, value, yes, no):
        jumps.append((len(code), yes, no))
        ins(op, value)

    ins(0x30, 0)  # LD B ABS
    ins(0x54, 0xf0)  # AND K
    jump(0x15, 0x40, 'ipv4', 'check6')
    label('check6')
    jump(0x15, 0x60, 'ipv6', 'reject')
    label('ipv4')
    ins(0x28, 6)
    jump(0x45, 0x3fff, 'reject', 'proto4')
    label('proto4')
    ins(0x30, 9)
    jump(0x15, 6, 'ports4', 'udp4')
    label('udp4')
    jump(0x15, 17, 'ports4', 'reject')
    label('ports4')
    ins(0xb1, 0)  # LDX B MSH: IPv4 IHL
    for offset in (0, 2):
        ins(0x48, offset)  # LD H IND
        for port in ports:
            jump(0x15, port, 'accept', None)
    ins(0x06, 0)
    label('ipv6')
    ins(0x30, 6)
    jump(0x15, 6, 'ports6', 'udp6')
    label('udp6')
    jump(0x15, 17, 'ports6', 'reject')
    label('ports6')
    for offset in (40, 42):
        ins(0x28, offset)
        for port in ports:
            jump(0x15, port, 'accept', None)
    label('reject')
    ins(0x06, 0)
    label('accept')
    ins(0x06, 65535)
    for index, yes, no in jumps:
        code[index][1] = labels[yes] - index - 1 if yes else 0
        code[index][2] = labels[no] - index - 1 if no else 0
    return code


class PacketSocket:
    def __init__(self, ports):
        if not hasattr(socket, 'AF_PACKET'):
            raise OSError('linux_packet_socket_unavailable')
        # Protocol zero receives nothing until the filter and timestamp option
        # are installed. No promiscuous membership and no send API.
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, 0)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            self.sock.setsockopt(socket.SOL_SOCKET, 35, 1)  # SO_TIMESTAMPNS (64-bit Linux ABI)

            class Instruction(ctypes.Structure):
                _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                            ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]

            class Program(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ushort), ('instructions', ctypes.POINTER(Instruction))]

            instructions = (Instruction * len(filter_program(ports)))(*(Instruction(*v) for v in filter_program(ports)))
            program = Program(len(instructions), instructions)
            self.sock.setsockopt(socket.SOL_SOCKET, 26, bytes(program))  # SO_ATTACH_FILTER
            self.sock.setsockopt(socket.SOL_SOCKET, 44, 1)  # SO_LOCK_FILTER
            # sockaddr_ll with ifindex=0 enables all local interfaces, without
            # the Python bind tuple's requirement for an interface name.
            class Address(ctypes.Structure):
                _fields_ = [('family', ctypes.c_ushort), ('protocol', ctypes.c_ushort),
                            ('index', ctypes.c_int), ('hatype', ctypes.c_ushort),
                            ('pkttype', ctypes.c_ubyte), ('length', ctypes.c_ubyte),
                            ('address', ctypes.c_ubyte * 8)]
            address = Address(socket.AF_PACKET, socket.htons(3), 0)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.bind(self.sock.fileno(), ctypes.byref(address), ctypes.sizeof(address)) != 0:
                raise OSError(ctypes.get_errno(), 'packet_bind_failed')
            self.sock.settimeout(.2)
        except BaseException:
            self.sock.close()
            raise

    def receive(self):
        data, ancillary, flags, address = self.sock.recvmsg(65535, 128)
        # Linux loopback presents outgoing+host copies of each packet. Keep
        # host copies only there; real interfaces need both traffic directions.
        if address[3] == 772 and address[2] == 4:
            return None
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError('truncated_capture')
        for level, kind, raw in ancillary:
            if level == socket.SOL_SOCKET and kind == 35 and len(raw) >= struct.calcsize('@ll'):
                seconds, nanos = struct.unpack_from('@ll', raw)
                return data, seconds + nanos / 1e9
        raise ValueError('missing_packet_timestamp')

    def drops(self):
        # PACKET_STATISTICS resets kernel counters on read; accumulate outside.
        _, drops = struct.unpack_from('II', self.sock.getsockopt(263, 6, 8))
        return drops

    def close(self):
        self.sock.close()
