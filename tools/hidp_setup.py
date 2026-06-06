#!/usr/bin/env python3
"""
Kernel HIDP tool: connect L2CAP to a DS4 and pass sockets to the kernel
via the HIDPCONNADD ioctl. The kernel then creates hidraw + input devices
and the hid-playstation driver handles LED/rumble.
"""
import socket, time, fcntl, ctypes, os, sys, subprocess, glob

ADDR = "0C:30:66:42:86:18"

# HIDPCONNADD = _IOW('H', 200, int) = 0x400448C8
HIDPCONNADD = 0x400448C8

class hidp_connadd_req(ctypes.Structure):
    _fields_ = [
        ("ctrl_sock", ctypes.c_int),
        ("intr_sock", ctypes.c_int),
        ("parser", ctypes.c_uint16),
        ("rd_size", ctypes.c_uint16),
        ("rd_data", ctypes.POINTER(ctypes.c_uint8)),
        ("country", ctypes.c_uint8),
        ("subclass", ctypes.c_uint8),
        ("vendor", ctypes.c_uint16),
        ("product", ctypes.c_uint16),
        ("version", ctypes.c_uint16),
        ("flags", ctypes.c_uint32),
        ("idle_to", ctypes.c_uint32),
        ("name", ctypes.c_char * 128),
    ]

# DS4 V2 Bluetooth HID report descriptor
DS4_BT_RD = bytes([
    0x05, 0x01, 0x09, 0x04, 0xa1, 0x01,
    0x85, 0x01, 0x05, 0x09, 0x19, 0x01, 0x29, 0x0e,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x0e, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x02, 0x75, 0x01, 0x81, 0x01,
    0x05, 0x01, 0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00,
    0x46, 0x3b, 0x01, 0x65, 0x14, 0x95, 0x01, 0x75, 0x08, 0x81, 0x42,
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
    0x15, 0x00, 0x26, 0xff, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
    0x06, 0x00, 0xff, 0x09, 0x20, 0x75, 0x08, 0x95, 0x4e, 0x81, 0x02,
    0x85, 0x05, 0x09, 0x21, 0x75, 0x08, 0x95, 0x1f, 0x91, 0x02,
    0x85, 0x11, 0x09, 0x22, 0x75, 0x08, 0x95, 0x4d, 0x91, 0x02,
    0x85, 0x81, 0x09, 0x23, 0x75, 0x08, 0x95, 0x06, 0xb1, 0x02,
    0x85, 0x12, 0x09, 0x24, 0x75, 0x08, 0x95, 0x0f, 0xb1, 0x02,
    0xc0,
])

def open_l2cap(psm, timeout_sec=3, retries=120):
    for i in range(retries):
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
            s.settimeout(timeout_sec)
            s.connect((ADDR, psm))
            return s
        except OSError:
            time.sleep(0.5)
    return None

def hidp_handshake(ctrl):
    for msg in [bytes([0x10]), bytes([0x71]), bytes([0x70, 0x00])]:
        try:
            ctrl.send(msg)
        except: pass
        time.sleep(0.01)

def main():
    print("=== DS4 Kernel HIDP Setup Tool ===", flush=True)

    ctrl = open_l2cap(0x11)
    if not ctrl:
        print("FAILED: L2CAP CTRL not connectable", flush=True)
        print("Put DS4 in pairing mode (hold PS+Share) and try again", flush=True)
        return 1
    print(f"CTRL OK", flush=True)

    hidp_handshake(ctrl)
    print("Handshake sent", flush=True)

    intr = open_l2cap(0x13, retries=10)
    if not intr:
        print("FAILED: L2CAP INTR not connectable", flush=True)
        ctrl.close()
        return 1
    print("INTR OK", flush=True)

    rd_buf = (ctypes.c_uint8 * len(DS4_BT_RD)).from_buffer_copy(DS4_BT_RD)

    hidp_sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, 6)
    req = hidp_connadd_req()
    req.ctrl_sock = ctrl.fileno()
    req.intr_sock = intr.fileno()
    req.parser = 0
    req.rd_size = len(DS4_BT_RD)
    req.rd_data = rd_buf
    req.country = 0
    req.subclass = 0
    req.vendor = 0x054c
    req.product = 0x09cc
    req.version = 0x0000
    req.flags = 0
    req.idle_to = 0
    req.name = b"Wireless Controller"

    try:
        fcntl.ioctl(hidp_sock.fileno(), HIDPCONNADD, req, False)
        print("HIDPCONNADD: SUCCESS", flush=True)
    except Exception as e:
        print(f"HIDPCONNADD FAILED: {e}", flush=True)
        ctrl.close()
        intr.close()
        hidp_sock.close()
        return 1

    print(" Waiting for kernel to create device...", flush=True)
    time.sleep(5)

    new_hidraw = [d for d in sorted(glob.glob('/dev/hidraw*'), key=lambda x: int(x[12:]))]
    print(f"Hidraw devices: {new_hidraw}", flush=True)

    try:
        with open('/proc/net/hidp') as f:
            print(f"HIDP sessions:\n{f.read()}", flush=True)
    except: pass

    try:
        r = subprocess.run(['sudo', 'dmesg'], capture_output=True, text=True, timeout=3)
        for line in r.stdout.split('\n')[-20:]:
            if any(x in line.lower() for x in ['hidp', 'dualshock', 'playstation', 'wireless', 'ds4']):
                print(f"  dmesg: {line.strip()}", flush=True)
    except: pass

    print("Connection alive for 30s. Did LED change? (Ctrl+C to exit)", flush=True)
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        pass

    ctrl.close()
    intr.close()
    hidp_sock.close()
    print("Done", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
