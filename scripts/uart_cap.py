#!/usr/bin/env python3
# usage: uart_cap.py <dev> <baud> <seconds> <outfile> -- raw capture at any baud (termios2 BOTHER)
import fcntl, os, struct, sys, time, termios
dev, baud, secs, out = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
TCGETS2, TCSETS2, BOTHER, CBAUD = 0x802C542A, 0x402C542B, 0o010000, 0o010017
fd = os.open(dev, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
buf = bytearray(fcntl.ioctl(fd, TCGETS2, bytes(44)))
i, o, c, l = struct.unpack_from('4I', buf)
c = (c & ~CBAUD & ~termios.CRTSCTS) | BOTHER | termios.CS8 | termios.CREAD | termios.CLOCAL
c &= ~(termios.PARENB | termios.CSTOPB)
struct.pack_into('4I', buf, 0, 0, 0, c, 0)
buf[17 + termios.VMIN] = 0; buf[17 + termios.VTIME] = 0
struct.pack_into('2I', buf, 36, baud, baud)
fcntl.ioctl(fd, TCSETS2, bytes(buf))
end = time.time() + secs
with open(out, 'wb') as f:
    while time.time() < end:
        try: d = os.read(fd, 4096)
        except BlockingIOError: d = b''
        if d: f.write(d); f.flush()
        else: time.sleep(0.01)
