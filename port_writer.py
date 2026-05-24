"""
Write mitmproxy's bound listen port to a file once the proxy is up.

Usage:
    mitmdump -p 0 -s port_writer.py --set port_file=/tmp/mitm.port
"""

import os
import tempfile

from mitmproxy import ctx


class PortWriter:
    def load(self, loader) -> None:
        loader.add_option("port_file", str, "", "Write the bound listen port here.")

    def running(self) -> None:
        path = ctx.options.port_file
        if not path:
            return
        proxyserver = ctx.master.addons.get("proxyserver")
        for server in proxyserver.servers:
            if not server.is_running or not server.listen_addrs:
                continue
            port = server.listen_addrs[0][1]
            d = os.path.dirname(os.path.abspath(path)) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".port.", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(f"{port}\n")
            os.replace(tmp, path)
            return


addons = [PortWriter()]
