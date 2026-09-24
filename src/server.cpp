#include <csignal>
#include <filesystem>
#include <limits>
#include <memory>
#include <string>

#include <httplib.h>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;
using namespace pybind11::literals;

namespace fs = std::filesystem;

/* Fallback MIME type */
static const std::string MIME_DEFAULT="text/plain";

/* Unfortunately, we need to carry around a global pointer just for signal handling. */
static std::unique_ptr<httplib::Server> svr = nullptr;
static void sigint(int signo) {
	if(svr)
		svr->stop();
}

static void run_server(py::object handler, int port=80, py::object webroot=py::none(), int max_age=3600)
{
	/* Can only run one server at a time */
	if (svr)
		throw std::runtime_error("Tuber server already running!");

	/*
	 * Start webserver
	 */

	svr = std::make_unique<httplib::Server>();
	std::signal(SIGINT, &sigint);

	/* A single long-lived keep-alive connection with a single client is
	 * the expected "hot path": don't cap the number of requests it can
	 * carry. The idle timeout is left at cpp-httplib's default (5s). */
	svr->set_keep_alive_max_count(std::numeric_limits<size_t>::max());
	svr->set_default_file_mimetype(MIME_DEFAULT);

	/* It's Always TCP_NODELAY. Every damn time.
	 * https://brooker.co.za/blog/2024/05/09/nagle.html */
	svr->set_tcp_nodelay(true);

	/* Set up /tuber endpoint.
	 *
	 * This serves both "hot" (method call) and "cold" paths (metadata,
	 * cached property fetches). All paths are coded in Python (in the
	 * tuber.server package), with hot path dispatch to C++ handled by the
	 * user. */
	svr->Post("/tuber", [handler](const httplib::Request& req, httplib::Response& res) {
		py::gil_scoped_acquire acquire;

		try {
			/* The request body is passed as bytes: it may
			 * be binary (CBOR), and every codec on the
			 * Python side accepts bytes. */
			py::tuple resp = handler(py::bytes(req.body),
					"content_type"_a=req.get_header_value("Content-Type"),
					"accept"_a=req.get_header_value("Accept"),
					"x_tuber_options"_a=req.get_header_value("X-Tuber-Options"));

			std::string response_format = resp[0].cast<std::string>();

			/* The response path is kept zero-copy using a string
			 * view (a raw buffer for cbor2/orjson, or utf-8 for
			 * json). This is legitimate until the GIL lock is
			 * released. */
			py::object body = resp[1];
			auto view = body.cast<std::string_view>();

			/* A zero-length provider would elide the Content-Length
			 * header entirely, leaving the response undelimited on
			 * a keep-alive connection. No codec emits zero bytes,
			 * but don't turn that assumption into a protocol bug. */
			if (view.empty()) {
				res.set_content("", response_format);
				return;
			}

			/* Because bytes/str are immutable and our reference
			 * pins the object, the worker thread may read the
			 * buffer without the GIL; only the release re-enters
			 * Python. cpp-httplib calls the releaser exactly once,
			 * on every path (including failed or abandoned
			 * writes). */
			auto keepalive = new py::object(std::move(body));
			res.set_content_provider(view.size(), response_format,
				[data = view.data()](size_t offset, size_t length, httplib::DataSink& sink) {
					return sink.write(data + offset, length);
				},
				[keepalive](bool) {
					py::gil_scoped_acquire acquire;
					delete keepalive;
				});
		} catch (py::error_already_set &e) {
			res.status = 500;
			res.set_content(std::string("Python exception: ") + e.what() + "\n", MIME_DEFAULT);
		}
	});

	/* The /tuber endpoint is POST-only. */
	svr->Get("/tuber", [](const httplib::Request&, httplib::Response& res) {
		res.status = 405;
	});

	/* If a valid webroot was provided, serve static content. */
	if (!webroot.is_none()) {
		fs::path root = fs::canonical(webroot.cast<std::string>());

		/* Vary is set on every static response, not just the compressed ones:
		 * a request for /foo.js may be answered from foo.js or foo.js.gz
		 * depending on Accept-Encoding, so caches must key on it either way. */
		if(!svr->set_mount_point("/", root.string(), {
					{"Cache-Control", "max-age="+std::to_string(max_age)},
					{"Vary", "Accept-Encoding"}}))
			throw std::runtime_error("Webroot is not a directory: " + root.string());

		/* The mount point only serves files that exist under their own name.
		 * Anything it declines lands here: if the request says gzip
		 * compressed transfers are acceptable, and we can find a file of the
		 * expected name with a '.gz' suffix, send that instead. */
		svr->Get(".*", [root, max_age](const httplib::Request& req, httplib::Response& res) {
			auto accept_header = req.get_header_value("Accept-Encoding");
			auto path = root / fs::path(req.path).relative_path();
			path += ".gz";

			/* The mount point resolves the request path against the webroot
			 * for us; since we're bypassing it, repeat its containment check
			 * (dot segments and symlinks alike) on our own path. */
			std::error_code ec;
			auto resolved = fs::weakly_canonical(path, ec);

			if(accept_header.empty() || accept_header.find("gzip")==std::string::npos ||
			   ec || *resolved.lexically_relative(root).begin() == ".." ||
			   !fs::is_regular_file(resolved)) {
				res.status = 404;
				res.set_content("No such file or directory.\n", MIME_DEFAULT);
				return;
			}

			/* use the original file extension to guess the data type! */
			res.set_file_content(resolved.string(), httplib::detail::find_content_type(
						resolved.stem().string(), {}, MIME_DEFAULT));
			res.set_header("Cache-Control", "max-age="+std::to_string(max_age));
			res.set_header("Content-Encoding", "gzip");

			/* This URL serves different bytes depending on Accept-Encoding.
			 * Say so, or a shared cache is entitled to hand the compressed
			 * response to a client that never asked for it. */
			res.set_header("Vary", "Accept-Encoding");
		});
	}

	/* Go! */
	bool ok;
	{
		py::gil_scoped_release release;
		ok = svr->listen("0.0.0.0", port);
	}

	/* Restore default signal disposition and release our handler
	 * reference while the interpreter is still alive. (cpp-httplib joins
	 * its worker threads before listen() returns.) */
	std::signal(SIGINT, SIG_DFL);
	svr.reset();

	if (!ok)
		throw std::runtime_error("Tuber server could not listen on port " + std::to_string(port));
}


PYBIND11_MODULE(_tuber_runtime, m) {
	m.doc() = "Tuber server runtime library";

	m.def("run_server", &run_server,
	    "Main server runtime function that creates a webserver with a static webroot\n"
	    "endpoint and a /tuber endpoint that parses requests via a handler function,\n"
	    "and runs the server until an interrupt is signaled.\n\n"
	    "Arguments\n---------\n"
	    "handler : callable\n"
	    "    Callable that takes an encoded request bytestring plus request headers,\n"
	    "    and returns the response format and encoded response string. Signature:\n"
	    "    ``function(request: bytes, *, content_type: str, accept: str,\n"
	    "    x_tuber_options: str) -> tuple[str, str]``\n"
	    "    Absent headers are passed as empty strings.\n"
	    "port : int\n"
	    "    Port on which to run the server\n"
	    "webroot : str\n"
	    "    Location to serve static content\n"
	    "max_age : int\n"
	    "    Maximum cache residency for static (file) assets\n",
	    py::arg("handler"), py::arg("port")=80, py::arg("webroot")=py::none(), py::arg("max_age")=3600);
}
