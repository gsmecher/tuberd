#!/usr/bin/env python3
"""
Example of running a tuber server and client in the same process.

No separate server is needed; run this script on its own:
    python example_embedded.py

Embedding the server this way is useful for tests, notebooks, and programs
that expose their own objects while also talking to them.
"""

import threading

import tuber
from tuber.server import Server

from example_server import registry

# Constructing a Server binds its port straight away.  Port 0 asks the kernel
# for any free port, and server.port reports the one it chose.  The socket is
# already listening, so clients may connect before serve() is called.
server = Server(registry, port=0)
print("Server bound to port", server.port)

# serve() blocks until stop() is called, so run it on a background thread.
# (On the main thread, it would also stop on Ctrl-C.)
thread = threading.Thread(target=server.serve)
thread.start()

try:
    with tuber.resolve_simple(f"localhost:{server.port}") as client:
        client.driver.set_knob(42)
        print("Knob:", client.driver.get_knob())

        with client.tuber_context() as ctx:
            ctx.driver.get_all()
            ctx.thermometer.read_temperature()
            state, temp = ctx()
        print("Driver:", state)
        print(f"Temperature: {temp:.2f} °C")

        # The client and server share this process, so the registry objects
        # can also be inspected directly, bypassing the network.
        print("Knob (read locally):", registry["driver"].knob)
finally:
    # stop() may be called from any thread; serve() returns once the server's
    # worker threads have finished.  A stopped server cannot be restarted.
    server.stop()
    thread.join()
    print("Server stopped")
