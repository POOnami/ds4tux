import sys, time, select, os
sys.path.insert(0, '/home/james/ds4tux/src')
from evdev import InputDevice, ecodes

for fn in sorted(os.listdir('/dev/input')):
    if not fn.startswith('event'): continue
    full = f'/dev/input/{fn}'
    try:
        dev = InputDevice(full)
        if 'Xbox' in dev.name:
            print(f'FOUND: {full} - {dev.name}')
            deadline = time.time() + 4
            count = 0
            while time.time() < deadline:
                r, _, _ = select.select([dev], [], [], 0.5)
                if r:
                    while True:
                        event = dev.read_one()
                        if event is None:
                            break
                        count += 1
                        if event.type != ecodes.EV_SYN and event.value != 128:
                            t = ecodes.EV.get(event.type, '?')
                            c = ecodes.ABS.get(event.code, ecodes.BTN.get(event.code, str(event.code)))
                            print(f'  #{count} {t} {c} = {event.value}')
            print(f'Total events: {count}')
            dev.close()
            break
    except (OSError, IOError):
        pass
