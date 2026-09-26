Example Interface
-----------------

This directory contains a small but complete demonstration of the tuber
client/server system.  The server exposes a few simulated devices — a
``DeviceDriver`` with a toggle button and adjustable knob, a ``Thermometer``
with configurable calibration, and an array of identical thermometers — and
two client scripts show how to reach them synchronously and asynchronously.
A third script runs the server and a client together in one process.

The examples use all of the optional client features, so install them with::

  pip install "tuberd[async,cbor,numpy]"

Starting the server
~~~~~~~~~~~~~~~~~~~

Run the server in its own shell.  The ``-p`` flag sets the port::

  python example_server.py -p 8080

The server prints the port it's running on.  Pass ``-p 0`` to use any free
port, for example when 8080 is already taken::

  python example_server.py -p 0
  Serving on port 52614

Both client scripts connect to port 8080 by default; pass ``-p`` to use
another::

  python example_client.py -p 52614

The registry is defined at module level, so ``tuberd`` can also load the
file directly::

  tuberd -r example_server.py -p 8080

The registry in ``example_server.py`` shows several patterns that appear in
real drivers:

* ``__tuber_object__ = True`` marks a class as a navigable tuber object.
* ``__tuber_exclude__`` hides attributes that cannot or should not be sent
  over the network (e.g. hardware handles, serial ports).  Clients access
  that state through explicit methods instead.
* ``__tuber_dynamic__`` lists dynamic properties (``Thermometer.offset``, a
  plain attribute, and ``Thermometer.temperature``, a ``@property``).
  Clients read them from the server on every access, and they are the only
  properties clients can set.  Any other attribute, such as ``label``, is
  static: clients cache its value when they connect.
* Exceptions raised by a method (``DeviceDriver.set_knob`` rejects values
  outside 0–100) are sent back to the client, and warnings emitted by a
  method (``Thermometer.set_calibration`` warns about large offsets) are
  re-emitted on the client.
* ``TuberArray`` exposes a list of identical objects (``sensors``) with a
  single shared description.  Because the description is shared, static
  properties come from the first item; per-item state is read through
  methods or dynamic properties.  Use ``TuberContainer`` for heterogeneous
  items.
* ``Thermometer.read_samples`` returns a numpy array, which clients can
  receive over CBOR.

Synchronous client
~~~~~~~~~~~~~~~~~~

The synchronous client is the simplest way to script a tuber server::

  python example_client.py

``tuber.resolve_simple()`` returns a proxy for the server's registry.
Attributes on the proxy correspond to named entries in the registry, and
method calls on those attributes translate directly to HTTP requests::

  client = tuber.resolve_simple("localhost:8080", timeout=5.0)
  driver = client.driver

  driver.set_knob(42)
  state = driver.get_all()   # returns a TuberResult (attribute-style access)
  print(state.knob)          # 42

Each open keep-alive connection occupies one of the server's worker threads.
A client's connections are closed once it is no longer referenced, but
``close()`` releases them immediately, for the client and every object
reached through it::

  client.close()

A client can also be used as a context manager, which closes it on exit.
This suits short-lived clients, such as one that returns plain Python dicts
(``convert_json=False``)::

  with tuber.resolve_simple("localhost:8080", convert_json=False) as client:
      state = client.driver.get_all()   # {"label": ..., "button": ..., "knob": ...}

Server-side exceptions are raised as ``tuber.TuberRemoteError``, whose message
includes the server-side traceback::

  try:
      driver.set_knob(500)
  except tuber.TuberRemoteError as e:
      print(e)

**Dynamic properties** are read from the server on every access.  Assigning
one sets it on the server, and ``tuber_set()`` does the same, returning the
value read back by the server.  Static properties are read-only::

  therm = client.thermometer
  therm.temperature                 # read from the server
  therm.offset = 0.5                # set on the server
  therm.tuber_set("offset", 1.5)    # 1.5
  therm.label = "renamed"           # AttributeError: label is static

Inside a context, ``tuber_get()`` and ``tuber_set()`` batch property reads
and writes with method calls::

  with therm.tuber_context() as ctx:
      ctx.tuber_set("offset", 2.0)
      ctx.tuber_get("temperature")
      ctx.get_calibration()
      results = ctx()

**Batching** multiple calls into a single HTTP request reduces round-trip
overhead.  Open a context manager on any proxy object and queue calls inside
it; nothing is sent until the context is flushed::

  with driver.tuber_context() as ctx:
      ctx.push_button()
      ctx.set_knob(7)
      ctx.get_all()
      results = ctx()   # one request, list of three results

Each queued call returns a future.  Calling ``.result()`` on it sends all the
calls queued so far, and any calls still queued when the context exits are
sent automatically::

  with driver.tuber_context() as ctx:
      knob = ctx.get_knob()
      ctx.set_knob(knob.result() + 1)   # get_knob() is sent here
      new_knob = ctx.get_knob()
  print(new_knob.result())              # set_knob() and get_knob() sent on exit

To batch calls across different registry objects, open the context on the
top-level client instead::

  with client.tuber_context() as ctx:
      ctx.driver.get_all()
      ctx.thermometer.read_temperature()
      results = ctx()

By default, the first failing call in a batch raises an exception and the
calls after it are skipped.  Pass ``return_exceptions=True`` to run every call
and get each failure back in place of its result.  This includes results the
server cannot serialize, such as a numpy array sent over JSON; the other
results in the batch are unaffected::

  with client.tuber_context() as ctx:
      ctx.driver.set_knob(500)            # raises on the server
      ctx.thermometer.read_samples(4)     # numpy array: not JSON serializable
      ctx.driver.get_knob()
      results = ctx(return_exceptions=True)
  # [TuberRemoteError(...), TuberRemoteError(...), 8]

**Arrays of objects** support ``len()``, indexing and iteration.
``tuber_call()`` calls a method on every item (or a subset, given ``keys``) in
a single request::

  sensors = client.sensors
  sensors[2].read_temperature()
  sensors.tuber_call("get_calibration")                  # [0.0, 0.1, 0.2, 0.3]
  sensors.tuber_call("read_temperature", keys=[0, 3])

``tuber_get()`` and ``tuber_set()`` do the same for dynamic properties.
``tuber_set()`` sets one value on every selected item, or one value per item
with ``values``::

  sensors.tuber_get("offset")                            # [0.0, 0.1, 0.2, 0.3]
  sensors.tuber_set("offset", 0.5, keys=[1, 2])          # [0.5, 0.5]
  sensors.tuber_set("offset", values=[0.0, 0.1, 0.2, 0.3])

  with client.tuber_context() as ctx:
      for i in range(len(sensors)):
          ctx.sensors[i].read_temperature()
      temps = ctx()

**CBOR and numpy**: clients accept JSON by default.  Requesting CBOR gives a
compact binary encoding in which numpy arrays are sent as typed arrays and
decoded back into numpy arrays::

  with tuber.resolve_simple("localhost:8080", accept_types=["application/cbor"]) as cbor_client:
      samples = cbor_client.thermometer.read_samples(8)   # numpy.ndarray

Asynchronous client
~~~~~~~~~~~~~~~~~~~

The async client is better suited to programs that already use ``asyncio``
or that need to overlap I/O with computation::

  python example_async_client.py

``await tuber.resolve()`` returns an async proxy; every method call is a
coroutine.  It accepts the same options as ``resolve_simple()``::

  client = await tuber.resolve("localhost:8080", timeout=5.0)
  driver = client.driver

  await driver.set_knob(42)
  state = await driver.get_all()

Reading a dynamic property also returns an awaitable.  Assignment can't be
awaited, so it raises ``tuber.TuberStateError``; use ``tuber_set()``
instead::

  temp = await therm.temperature
  await therm.tuber_set("offset", 1.5)

Async clients share one HTTP session per event loop, which is closed along
with the loop (e.g. when ``asyncio.run()`` returns), so they need no
``close()``.

Async batching uses ``async with``.  Queued calls return awaitable futures;
awaiting one sends every call queued so far::

  async with driver.tuber_context() as ctx:
      ctx.push_button()
      ctx.set_knob(7)
      ctx.get_all()
      results = await ctx()   # one request, list of three results

  async with driver.tuber_context() as ctx:
      ctx.push_button()
      knob = await ctx.get_knob()   # sends push_button() and get_knob() together

Batch options such as ``return_exceptions`` can also be set when the context
is created::

  async with client.tuber_context(return_exceptions=True) as ctx:
      ...

For calls that are fully independent, ``asyncio.gather`` dispatches them
concurrently — all requests are in flight at the same time::

  button, knob, temp = await asyncio.gather(
      driver.get_button(),
      driver.get_knob(),
      therm.read_temperature(),
  )

  temps = await asyncio.gather(*(s.read_temperature() for s in client.sensors))

Embedded server
~~~~~~~~~~~~~~~

``example_embedded.py`` runs the server and a client in the same process, so
no separate server is needed::

  python example_embedded.py

This is useful for tests, notebooks, and programs that expose their own
objects while also talking to them.  Constructing a ``tuber.server.Server``
binds its port immediately; ``port=0`` picks any free port, and
``server.port`` reports it.  ``serve()`` blocks until ``stop()`` is called,
so it runs on a background thread::

  import threading
  from tuber.server import Server

  server = Server(registry, port=0)
  thread = threading.Thread(target=server.serve)
  thread.start()
  try:
      with tuber.resolve_simple(f"localhost:{server.port}") as client:
          client.driver.set_knob(42)
  finally:
      server.stop()
      thread.join()

A stopped server cannot be restarted; create a new ``Server`` instead.
