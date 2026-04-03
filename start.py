#!/usr/bin/env python3
"""
Usage:
  python start.py 1              - server only
  python start.py 2              - server + tunnel
  python start.py 1 --port 8080
  python start.py 2 --port 8080
"""

import subprocess
import sys
import threading

def stream(proc, prefix):
    for line in iter(proc.stdout.readline, b""):
        print(f"{prefix}{line.decode(errors='replace')}", end="", flush=True)

def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("1", "2"):
        print(__doc__)
        sys.exit(1)

    mode = args[0]
    extra = args[1:]   # e.g. --port 8080

    procs = []

    try:
        server = subprocess.Popen(
            [sys.executable, "-u", "server.py"] + extra,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        procs.append(server)
        threading.Thread(target=stream, args=(server, "[server] "), daemon=True).start()

        if mode == "2":
            tunnel = subprocess.Popen(
                [sys.executable, "-u", "create_tunnel.py"] + extra,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            procs.append(tunnel)
            threading.Thread(target=stream, args=(tunnel, "[tunnel] "), daemon=True).start()

        # Wait — exits when all processes finish, or on Ctrl+C
        for p in procs:
            p.wait()

    except KeyboardInterrupt:
        print("\nStopping...")
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()

if __name__ == "__main__":
    main()
