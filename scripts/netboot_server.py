#!/usr/bin/env python3
"""Boot server for the SoC's network loader (he-soc software/netboot): answers the loader's broadcast discovery
("ALSAQR-NETBOOT?") and serves files from one directory with TFTP (RFC 1350, blksize option) on an unprivileged
port, so it runs as a normal user next to the campus DHCP server.

  netboot_server.py --dir DIR [--port 6969] [--once] [--quiet]

The loader asks for alsaqr.dtb and fw_payload.bin. With --once the server exits after the payload has been
served completely (the loader jumps into it right after). Prints one line per event; "SERVED <file> <bytes> <ms>"
is what callers parse. It also accepts TFTP writes (WRQ, e.g. busybox `tftp -p -l FILE -r NAME HOST 6969` from
Linux on the board) into the same directory: "STORED <file> <bytes> <ms>".
"""
import argparse, os, select, socket, struct, sys, time

MAGIC_Q = b"ALSAQR-NETBOOT?"
MAGIC_A = b"ALSAQR-NETBOOT"


def log(quiet, *a):
    if not quiet:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


class Transfer:
    """One TFTP read in progress, on its own socket (the transfer id); lock-step, up to 5 retransmissions."""

    def __init__(self, path, peer, blksize, quiet):
        self.path, self.peer, self.blksize, self.quiet = path, peer, blksize, quiet
        self.f = open(path, "rb")
        self.size = os.fstat(self.f.fileno()).st_size
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.block = 0          # last block sent
        self.data = b""
        self.last = False
        self.sent_at = 0.0
        self.retries = 0
        self.t0 = time.time()
        self.done = False

    def send_block(self, n):
        self.sock.sendto(struct.pack("!HH", 3, n & 0xFFFF) + self.data, self.peer)
        self.sent_at = time.time()

    def start(self):
        # OACK for the options we accepted, then wait for ACK 0 before the first data block
        self.sock.sendto(b"\x00\x06" + b"blksize\x00" + str(self.blksize).encode() + b"\x00"
                         + b"tsize\x00" + str(self.size).encode() + b"\x00", self.peer)
        self.sent_at = time.time()

    def next_block(self):
        self.data = self.f.read(self.blksize)
        self.block += 1
        self.last = len(self.data) < self.blksize
        self.send_block(self.block)

    def on_packet(self, pkt, peer):
        if peer != self.peer or len(pkt) < 4:
            return
        op, n = struct.unpack("!HH", pkt[:4])
        if op == 5:
            log(self.quiet, "ERROR from client:", pkt[4:].decode(errors="replace"))
            self.done = True
            return
        if op != 4:
            return
        if n == (self.block & 0xFFFF):
            self.retries = 0
            if self.last:
                ms = int((time.time() - self.t0) * 1000)
                print(f"SERVED {os.path.basename(self.path)} {self.size} {ms}", flush=True)
                self.done = True
            else:
                self.next_block()

    def on_timeout(self):
        if time.time() - self.sent_at < 1.0:
            return
        self.retries += 1
        if self.retries > 5:
            log(self.quiet, "TIMEOUT", os.path.basename(self.path), "at block", self.block)
            self.done = True
            return
        if self.block == 0:
            self.start()
        else:
            self.send_block(self.block)

    def close(self):
        self.sock.close()
        self.f.close()


class Upload:
    """One TFTP write in progress (WRQ): ACK 0 (or OACK), then data blocks in lock step; done at a short block."""

    def __init__(self, path, peer, blksize, quiet):
        self.path, self.peer, self.blksize, self.quiet = path, peer, blksize, quiet
        self.f = open(path, "wb")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.block = 0          # last block received
        self.size = 0
        self.t0 = time.time()
        self.sent_at = 0.0
        self.retries = 0
        self.done = False
        self.oack = False

    def ack(self):
        if self.block == 0 and self.oack:
            self.sock.sendto(b"\x00\x06" + b"blksize\x00" + str(self.blksize).encode() + b"\x00", self.peer)
        else:
            self.sock.sendto(struct.pack("!HH", 4, self.block & 0xFFFF), self.peer)
        self.sent_at = time.time()

    def start(self, oack):
        self.oack = oack
        self.ack()

    def on_packet(self, pkt, peer):
        if peer != self.peer or len(pkt) < 4:
            return
        op, n = struct.unpack("!HH", pkt[:4])
        if op == 5:
            log(self.quiet, "ERROR from client:", pkt[4:].decode(errors="replace"))
            self.done = True
            return
        if op != 3:
            return
        if n == ((self.block + 1) & 0xFFFF):
            self.block += 1
            self.f.write(pkt[4:])
            self.size += len(pkt) - 4
            self.retries = 0
            self.ack()
            if len(pkt) - 4 < self.blksize:
                self.f.close()
                ms = int((time.time() - self.t0) * 1000)
                print(f"STORED {os.path.basename(self.path)} {self.size} {ms}", flush=True)
                self.done = True
        elif n == (self.block & 0xFFFF):
            self.ack()              # a repeated block: our ACK was lost

    def on_timeout(self):
        if time.time() - self.sent_at < 1.0:
            return
        self.retries += 1
        if self.retries > 5:
            log(self.quiet, "TIMEOUT", os.path.basename(self.path), "at block", self.block)
            self.done = True
            return
        self.ack()

    def close(self):
        self.sock.close()
        if not self.f.closed:
            self.f.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, default=6969)
    ap.add_argument("--once", action="store_true", help="exit after fw_payload.bin has been served")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    root = os.path.realpath(a.dir)
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ctl.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ctl.bind(("0.0.0.0", a.port))
    print(f"LISTENING {a.port} {root}", flush=True)
    transfers = []
    while True:
        socks = [ctl] + [t.sock for t in transfers]
        r, _, _ = select.select(socks, [], [], 0.2)
        for s in r:
            pkt, peer = s.recvfrom(65536)
            if s is ctl:
                if pkt.startswith(MAGIC_Q):
                    ctl.sendto(MAGIC_A, peer)
                    log(a.quiet, "DISCOVER from", peer[0])
                    continue
                if len(pkt) >= 4 and pkt[:2] == b"\x00\x01":          # RRQ
                    parts = pkt[2:].split(b"\x00")
                    name = parts[0].decode(errors="replace")
                    opts = dict(zip([p.decode().lower() for p in parts[2:-1:2]], [p.decode() for p in parts[3::2]]))
                    path = os.path.abspath(os.path.join(root, name))        # symlinks in DIR are fine; no escaping it
                    if not path.startswith(root + os.sep) or not os.path.isfile(path):
                        ctl.sendto(b"\x00\x05\x00\x01" + b"file not found\x00", peer)
                        log(a.quiet, "RRQ", name, "from", peer[0], "-> not found")
                        continue
                    blksize = max(8, min(int(opts.get("blksize", "512")), 65464))
                    t = Transfer(path, peer, blksize, a.quiet)
                    transfers.append(t)
                    log(a.quiet, "RRQ", name, f"({t.size} bytes, blksize {blksize}) from {peer[0]}:{peer[1]}")
                    t.start()
                    continue
                if len(pkt) >= 4 and pkt[:2] == b"\x00\x02":          # WRQ: store the file in DIR
                    parts = pkt[2:].split(b"\x00")
                    name = os.path.basename(parts[0].decode(errors="replace"))
                    opts = dict(zip([p.decode().lower() for p in parts[2:-1:2]], [p.decode() for p in parts[3::2]]))
                    if not name or name.startswith("."):
                        ctl.sendto(b"\x00\x05\x00\x02" + b"bad name\x00", peer)
                        continue
                    blksize = max(8, min(int(opts.get("blksize", "512")), 65464))
                    t = Upload(os.path.join(root, name), peer, blksize, a.quiet)
                    transfers.append(t)
                    log(a.quiet, "WRQ", name, f"(blksize {blksize}) from {peer[0]}:{peer[1]}")
                    t.start("blksize" in opts)
                    continue
            else:
                for t in transfers:
                    if t.sock is s:
                        t.on_packet(pkt, peer)
        for t in transfers:
            t.on_timeout()
        finished = [t for t in transfers if t.done]
        for t in finished:
            transfers.remove(t)
            t.close()
            if a.once and os.path.basename(t.path) == "fw_payload.bin":
                return 0


if __name__ == "__main__":
    sys.exit(main())
