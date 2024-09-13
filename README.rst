.. image:: https://badge.fury.io/py/tuberd.svg
   :target: https://badge.fury.io/py/tuberd

.. image:: https://github.com/gsmecher/tuberd/actions/workflows/package.yml/badge.svg
   :target: https://github.com/gsmecher/tuberd/actions/workflows/package.yml

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

- Pythonic call styles, including \*args, \*\*kwargs, and DocStrings.

- JSON and CBOR support for efficient and friendly serialization of return
  values.

- "Less-is-more" approach to code. For example, Tuber uses pybind11_ and C++ as
  a shim between C and Python, because the combination gives us the shortest
  and most expressive way to produce the results we want. It pairs excellently
  with orjson_ (as a JSON interface) or cbor2_ (as a CBOR interface), which
  efficiently serialize (for example) NumPy_ arrays created in C++ across the
  network.

- Schema-less RPC using standard-ish protocols (HTTP 1.1, JSON, CBOR, and
  something like JSON-RPC_). Avoiding a schema allows server and client code to
  be independently and seamlessly up- and downgraded, with differences between
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

Tuber is licensed using the 3-clause BSD license (BSD-3-Clause). This software
is intended to be useful, and its licensing is intended to be pragmatic. If
licensing is a stumbling block for you, please contact me at
`gsmecher@threespeedlogic.com <mailto:gsmecher@threespeedlogic.com>`_.

.. _Tuber: https://github.com/gsmecher/tuber
.. _GPLv3: https://www.gnu.org/licenses/gpl-3.0.en.html
.. _Jupyter: https://jupyter.org/
.. _IPython: https://ipython.org/
.. _libhttpserver: https://github.com/etr/libhttpserver
.. _NumPy: https://www.numpy.org
.. _orjson: https://github.com/ijl/orjson
.. _cbor2: https://github.com/agronholm/cbor2
.. _libmicrohttpd: https://www.gnu.org/software/libmicrohttpd/
.. _JSON-RPC: https://www.jsonrpc.org/
.. _pybind11: https://pybind11.readthedocs.io/en/stable/index.html
.. _C10K: http://www.kegel.com/c10k.html
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _aiohttp: https://docs.aiohttp.org/en/stable/
.. _autoawait: https://ipython.readthedocs.io/en/stable/interactive/autoawait.html

Installation
------------

Pre-built wheels for Linux and macOS operating systems are available on PyPI for
CPython 3.8+:

.. code:: bash

   pip install tuberd

Building from source requires the ``libmicrohttpd`` and ``libhttpserver``
dependencies.  To simplify the build process, the
``wheels/install_deps.sh`` script can be used to build all the dependencies
locally and compile against them.  In this instance, ``cmake`` should be able to
discover the appropriate paths for all dependencies.  Use the ``BUILD_DEPS``
``cmake`` argument to trigger this build with pip:

.. code:: bash

   CMAKE_ARGS="-DBUILD_DEPS=yes" pip install tuberd

If you prefer to build the dependencies manually, to ensure that ``cmake`` can
find the ``libhttpserver`` library, you may need to add the path where the
``FindLibHttpServer.cmake`` file is installed to the ``CMAKE_MODULE_PATH``
option, for example:

.. code:: bash

   CMAKE_ARGS="-DCMAKE_MODULE_PATH=/usr/local/share/cmake/Modules" pip install tuberd

Optional dependencies may be installed to enable alternative encoding schemes (cbor, orjson)
with and without numpy support, or the asyncio-enabled client interface:

.. code:: bash

   pip install tuberd[async,cbor,numpy,orjson]

To run the test suite, install the development dependencies:

.. code:: bash

   pip install tuberd[dev]

Client Installation
-------------------

The above ``tuberd`` package includes both the server and client components.
If you require just the python components to run the client interface,
pre-built wheels of the client code are available on PyPI for Python 3.

.. code:: bash

   pip install tuber-client

To include the dependencies for the asyncio-enabled interface and/or cbor
encoding with or without numpy support:

.. code:: bash

   pip install tuber-client[async,cbor,numpy]

Benchmarking
------------

With concurrency 1 and keep-alive enabled, a 1M request benchmark can be
generated as follows:

.. code:: bash

   $ sudo apt-get install apache2-utils
   $ echo '{ "object":"Wrapper", "method":"increment", "args":[[
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10,
        1,2,3,4,5,6,7,8,9,10 ]]}' > benchmark.json
   $ for n in `seq 100`
     do
         ab -q -k -n 10000 -c 1 -p benchmark.json -T application/json http://localhost:8080/tuber
     done | awk '
        BEGIN { delete A }
        /Time taken/ { A[length(A)+1] = $5; }
        END { printf("x = [ "); for(i in A) printf(A[i] ", "); print "];" }'

These results are formatted suitably for the following Python snippet:

.. code:: python

   import matplotlib.pyplot as plt
   plt.hist(x)
   plt.legend()
   plt.grid(True)
   plt.savefig('histogram.png')
