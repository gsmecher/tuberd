Tuber Server and Client
=======================

Tuber_ is a C++ server and Python client for exposing an instrumentation
control plane across a network.

On a client, you can write Python code like this:

.. code:: python

   >>> some_resource.increment([1, 2, 3, 4, 5])
   [2, 3, 4, 5, 6]

...and end up with a remote method call on a networked resource written in
Python or (more usually) C++. The C++ implementation might look like this:

.. code:: c++

   class SomeResource {
   public:
       std::vector<int> increment(std::vector<int> x) {
           std::ranges::for_each(x, [](int &n) { n++; });
           return x;
       };
   };

On the client side, Python needs to know where to find the server. On the
server side, the C++ code must be registered with pybind11 (just as any other
pybind11 code) and the tuber server.  Other than that, however, there is no
ceremony and no boilerplate.

Its main features and design principles are:

- Pythonic call styles, including *args, **kwargs, and DocStrings.

- "Less-is-more" approach to code. For example, Tuber uses pybind11_ and C++ as
  a shim between C and Python, because the combination gives us the shortest
  and most expressive way to produce the results we want. It pairs excellently
  with orjson_ as a JSON interface, which efficiently serializes (for example)
  NumPy_ arrays created in C++ across the network.

- Schema-less RPC using standard-ish protocols (HTTP 1.1, JSON, and something
  like JSON-RPC_). Avoiding a schema allows server and client code to be
  independently and seamlessly up- and downgraded, with differences between
  exposed APIs only visible at the sites where RPC calls are made.

- A mature, quick-ish, third-party, low-overhead, low-prerequisite embedded
  webserver. Tuber uses libhttpserver_, which in turn, is a C++ wrapper around
  the well-established libmicrohttpd_. We use the thread-per-connection
  configuration because a single keep-alive connection with a single client is
  the expected "hot path"; C10K_-style server architectures wouldn't be better.

- High performance when communicating with RPC endpoints, using:

  - HTTP/1.1 Keep-Alive to avoid single-connection-per-request overhead.  See
    `this Wikipedia page
    <https://en.wikipedia.org/wiki/HTTP_persistent_connection#HTTP_1.1>`_ for
    details.

  - A call API that (optionally) allows multiple RPC calls to be combined and
    dispatched together.

  - Client-side caches for remote properties (server-side constants)

  - Python 3.x's aiohttp_/asyncio_ libraries to asynchronously dispatch across
    multiple endpoints (e.g. multiple boards in a crate, each of which is an
    independent Tuber endpoint.)

- A friendly interactive experience using Jupyter_/IPython_-style REPL
  environments. Tuber servers export metadata that can be used to provide
  DocStrings and tab-completion for RPC resources.

- The ability to serve a web-based UI using static JavaScript, CSS, and HTML.

Anti-goals of this Tuber server include the following:

- No authentication/encryption is used. For now, network security is strictly
  out-of-scope. (Yes, it feels naïve to write this in 2022.)

- The additional complexity of HTTP/2 and HTTP/3 protocols are not justified.
  HTTP/1.1 keep-alive obviates much of the performance gains promised by
  connection multiplexing.

- The performance gains possible using a binary RPC protocol do not justify the
  loss of a human-readable, browser-accessible JSON protocol.

- The use of newer, better languages than C++ (server side) or Python (client
  side).  The instruments Tuber targets are likely to be a polyglot stew, and I
  am mindful that every additional language or runtime reduces the project's
  accessibility to newcomers.  Perhaps pybind11_ will be eclipsed by something
  in Rust one day - for now, the ability to make casual cross-language calls is
  essential to keeping Tuber small. (Exception: the orjson JSON library is a
  wonderful complement to tuber and I recommend using them together!)

Although the Tuber server hosts an embedded Python interpreter and can expose
embedded resources coded in ordinary Python, it is intended to expose C/C++
code. The Python interpeter provides a convenient, Pythonic approach to
attribute and method lookup and dispatch without the overhead of a fully
interpreted embedded runtime.

Tuber is licensed using the GNU GPLv3_ license. This license is inconveniently
restrictive, and is intended as a placeholder until I determine which of the
myriad of more permissive licenses is most appropriate.  Specifically, I intend
to relax licensing in order to allow third parties to use the Tuber server and
client with closed-source client codebases. (Note that open-source licenses, in
general, create an obligation to make source code available - but only where
compiled programs are distributed. You do not generally have an obligation to
publicly disclose source code.) If licensing is an issue and this text is still
present, you are strongly encouraged to contact Graeme Smecher at
`gsmecher@threespeedlogic.com <mailto:gsmecher@threespeedlogic.com>`_.

.. _Tuber: https://github.com/gsmecher/tuber
.. _GPLv3: https://www.gnu.org/licenses/gpl-3.0.en.html
.. _Jupyter: https://jupyter.org/
.. _IPython: https://ipython.org/
.. _libhttpserver: https://github.com/etr/libhttpserver
.. _NumPy: https://www.numpy.org
.. _orjson: https://github.com/ijl/orjson
.. _libmicrohttpd: https://www.gnu.org/software/libmicrohttpd/
.. _JSON-RPC: https://www.jsonrpc.org/
.. _pybind11: https://pybind11.readthedocs.io/en/stable/index.html
.. _C10K: http://www.kegel.com/c10k.html
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _aiohttp: https://docs.aiohttp.org/en/stable/
.. _autoawait: https://ipython.readthedocs.io/en/stable/interactive/autoawait.html
