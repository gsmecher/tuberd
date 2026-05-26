Example Interface
-----------------

The tuber interface consists of a service-side python interface, and a corresponding client interface.

The server runs as a persistent process on the remote device (e.g. a Rasberry Pi or similar), which has some operating system (typically a Linux flavor) that can run python processes.  To try this example, you can simply run the server script in a separate shell on your laptop::

  python example_server.py -p 8080

This will start the tuber server, listening on the default port 8080 for client connections.  The `registry` dictionary in the example script contains instances of each driver you'd like to include in the interface.  In this example, there is a single device, mapped to the `"driver"` entry in the registry.

The client interface runs on any other machine on your network that can connect to port 80 on your laptop. For this example, you can simply run the example client script in a separate python session on your laptop::

  python example_client.py

Notice that the user interface of the client connection mirrors all of the methods that are available in the driver class that's constructed on the server.
