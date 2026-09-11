Example Interface
-----------------

This directory contains a small but complete demonstration of the tuber
client/server system.  The server exposes two simulated devices — a
``DeviceDriver`` with a toggle button and adjustable knob, and a
``Thermometer`` with configurable calibration — and two client scripts show
how to reach them synchronously and asynchronously.

Starting the server
~~~~~~~~~~~~~~~~~~~

Run the server in its own shell.  The ``-p`` flag sets the port::

  python example_server.py -p 8080

The registry in ``example_server.py`` shows two patterns that appear in real
drivers:

* ``__tuber_object__ = True`` marks a class as a navigable tuber object.
* ``__tuber_exclude__`` hides attributes that cannot or should not be sent
  over the network (e.g. hardware handles, serial ports).  Clients access
  that state through explicit methods instead.

Synchronous client
~~~~~~~~~~~~~~~~~~

The synchronous client is the simplest way to script a tuber server::

  python example_client.py

``tuber.resolve_simple()`` returns a proxy for the server's registry.
Attributes on the proxy correspond to named entries in the registry, and
method calls on those attributes translate directly to HTTP requests::

  client = tuber.resolve_simple("localhost:8080")
  driver = client.driver

  driver.set_knob(42)
  state = driver.get_all()   # returns a TuberResult (attribute-style access)
  print(state.knob)          # 42

Pass ``convert_json=False`` if you prefer plain Python dicts::

  client = tuber.resolve_simple("localhost:8080", convert_json=False)
  state = client.driver.get_all()   # {"label": ..., "button": ..., "knob": ...}

**Batching** multiple calls into a single HTTP request reduces round-trip
overhead.  Open a context manager on any proxy object and queue calls inside
it; nothing is sent until the context is flushed::

  with driver.tuber_context() as ctx:
      ctx.push_button()
      ctx.set_knob(7)
      ctx.get_all()
      results = ctx()   # one request, list of three results

To batch calls across different registry objects, open the context on the
top-level client instead::

  with client.tuber_context() as ctx:
      ctx.driver.get_all()
      ctx.thermometer.read_temperature()
      results = ctx()

Asynchronous client
~~~~~~~~~~~~~~~~~~~

The async client is better suited to programs that already use ``asyncio``
or that need to overlap I/O with computation::

  python example_async_client.py

``await tuber.resolve()`` returns an async proxy; every method call is a
coroutine::

  client = await tuber.resolve("localhost:8080")
  driver = client.driver

  await driver.set_knob(42)
  state = await driver.get_all()

Async batching uses ``async with``::

  async with driver.tuber_context() as ctx:
      ctx.push_button()
      ctx.set_knob(7)
      ctx.get_all()
      results = await ctx()   # one request, list of three results

For calls that are fully independent, ``asyncio.gather`` dispatches them
concurrently — all requests are in flight at the same time::

  button, knob, temp = await asyncio.gather(
      driver.get_button(),
      driver.get_knob(),
      therm.read_temperature(),
  )
