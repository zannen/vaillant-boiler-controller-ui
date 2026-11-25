import socket


class Ebusd:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("rw", newline="\n")

    def read(self, name):
        return self.command(f"read -f -c bai {name}")

    def write(self, name, value):
        """Write a message; ebusd answers "done" or "empty" on success,
        "ERR: ..." on failure."""
        return self.command(f"write -c bai {name} {value}")

    def close(self):
        self.sock.close()

    def command(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        lines = []
        while True:  # ebusd ends each response with an empty line
            line = self.f.readline()
            if not line:
                raise ConnectionError("ebusd closed the connection")
            line = line.rstrip("\n")
            if not line:
                return " ".join(lines)
            lines.append(line)
