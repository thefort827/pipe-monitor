"""
Minimal telnetlib compatibility shim for Python 3.13+.
telnetlib was removed from the standard library in Python 3.13.
This provides a stub Telnet class for wechaty compatibility.
"""
import socket
import time


class Telnet:
    """Minimal Telnet stub for wechaty compatibility."""

    def __init__(self, host=None, port=0, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        if host is not None:
            self.open(host, port, timeout)

    def open(self, host, port=0, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def read_until(self, expected, timeout=None):
        data = b''
        while True:
            chunk = self.sock.recv(1024)
            if not chunk:
                break
            data += chunk
            if expected in data:
                break
        return data

    def read_all(self):
        data = b''
        while True:
            try:
                chunk = self.sock.recv(1024)
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
        return data

    def write(self, buffer):
        self.sock.sendall(buffer)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
