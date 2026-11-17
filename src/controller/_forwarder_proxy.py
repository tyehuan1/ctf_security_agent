"""Minimal TCP proxy run inside the forwarder container (python3 -c <this source>).

argv: LISTEN_PORT REMOTE_HOST REMOTE_PORT

Listens on 0.0.0.0:LISTEN_PORT and, for each client, opens a connection to REMOTE_HOST:REMOTE_PORT
and pumps bytes both ways. This is the only hop with egress: the solver sandbox reaches it as the
host `target` over the internal network, and it reaches the real challenge over the egress network.
It holds no secret and never touches the solver workspace. Pure stdlib so it runs in the existing
sandbox image with no extra packages.
"""

import socket
import sys
import threading


def _pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client, remote_host, remote_port):
    try:
        upstream = socket.create_connection((remote_host, remote_port))
    except OSError as err:
        client.close()
        sys.stderr.write(f"forwarder: upstream connect failed: {err}\n")
        return
    threading.Thread(target=_pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_pump, args=(upstream, client), daemon=True).start()


def main():
    listen_port = int(sys.argv[1])
    remote_host = sys.argv[2]
    remote_port = int(sys.argv[3])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(64)
    sys.stderr.write(f"forwarder: {listen_port} -> {remote_host}:{remote_port}\n")
    sys.stderr.flush()
    while True:
        client, _ = srv.accept()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_handle, args=(client, remote_host, remote_port), daemon=True).start()


if __name__ == "__main__":
    main()
