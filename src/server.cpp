#include <atomic>
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

class Server;

/* The SIGINT handler needs a way back to the server it should stop. Only a
 * server that installed the handler (serve() with handle_sigint) is
 * registered here, and only for as long as it is serving. */
static std::atomic<Server*> sigint_target = nullptr;

/*
 * A tuber server. Construction binds the port (so port() is valid, and a
 * client told about it can connect, as soon as the constructor returns);
 * serve() runs the accept loop until stop() is called from another thread
 * or, when handling SIGINT, the process is interrupted.
 */
class Server {
public:
	Server(py::object handler, int port, py::object webroot, int max_age);
	~Server();

	int port() const { return port_; }
	void serve(bool handle_sigint);
	void stop();

private:
	std::unique_ptr<httplib::Server> svr_;
	int port_ = -1;
	bool served_ = false;

	/* Distinguishes a requested stop (which makes listen_after_bind()
	 * return false if it lands before the accept loop starts) from a
	 * genuine failure to listen. */
	std::atomic<bool> stop_requested_ = false;
};

static void sigint(int signo) {
	if (Server *s = sigint_target.load())
		s->stop();
}

Server::Server(py::object handler, int port, py::object webroot, int max_age)
	: svr_(std::make_unique<httplib::Server>())
{
	/* A single long-lived keep-alive connection with a single client is
	 * the expected "hot path": don't cap the number of requests it can
	 * carry. The idle timeout is left at cpp-httplib's default (5s). */
	svr_->set_keep_alive_max_count(std::numeric_limits<size_t>::max());
	svr_->set_default_file_mimetype(MIME_DEFAULT);

	/* It's Always TCP_NODELAY. Every damn time.
	 * https://brooker.co.za/blog/2024/05/09/nagle.html */
	svr_->set_tcp_nodelay(true);

	/* Bind exclusively. cpp-httplib's default is SO_REUSEPORT (on Linux and
	 * macOS), which lets a second server bind a port that already has a
	 * live listener and silently shares connections between the two - so a
	 * stray tuberd steals traffic from its replacement instead of failing
	 * to start. SO_REUSEADDR still lets a restarted server bind past
	 * TIME_WAIT connections left by its predecessor, but rejects a live
	 * listener. On Windows SO_REUSEADDR alone allows the sharing too;
	 * SO_EXCLUSIVEADDRUSE is the equivalent there. */
	svr_->set_socket_options([](socket_t sock) {
#ifdef _WIN32
		httplib::set_socket_opt(sock, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, 1);
#else
		httplib::set_socket_opt(sock, SOL_SOCKET, SO_REUSEADDR, 1);
#endif
	});

	/* Set up /tuber endpoint.
	 *
	 * This serves both "hot" (method call) and "cold" paths (metadata,
	 * cached property fetches). All paths are coded in Python (in the
	 * tuber.server package), with hot path dispatch to C++ handled by the
	 * user. */
	svr_->Post("/tuber", [handler](const httplib::Request& req, httplib::Response& res) {
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
	svr_->Get("/tuber", [](const httplib::Request&, httplib::Response& res) {
		res.status = 405;
	});

	/* If a valid webroot was provided, serve static content. */
	if (!webroot.is_none()) {
		fs::path root = fs::canonical(webroot.cast<std::string>());

		/* Vary is set on every static response, not just the compressed ones:
		 * a request for /foo.js may be answered from foo.js or foo.js.gz
		 * depending on Accept-Encoding, so caches must key on it either way. */
		if(!svr_->set_mount_point("/", root.string(), {
					{"Cache-Control", "max-age="+std::to_string(max_age)},
					{"Vary", "Accept-Encoding"}}))
			throw std::runtime_error("Webroot is not a directory: " + root.string());

		/* The mount point only serves files that exist under their own name.
		 * Anything it declines lands here: if the request says gzip
		 * compressed transfers are acceptable, and we can find a file of the
		 * expected name with a '.gz' suffix, send that instead. */
		svr_->Get(".*", [root, max_age](const httplib::Request& req, httplib::Response& res) {
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

	/* Bind now, serve later. cpp-httplib's bind step also calls listen(),
	 * so from here on the kernel queues connections for us: a client that
	 * learns the port from port() can connect before serve() is called.
	 * Port 0 asks the kernel for any free port. */
	port_ = (port == 0) ? svr_->bind_to_any_port("0.0.0.0")
	                    : (svr_->bind_to_port("0.0.0.0", port) ? port : -1);

	if (port_ < 0)
		throw std::runtime_error("Tuber server could not bind to port " + std::to_string(port));
}

Server::~Server()
{
	/* Release the listening socket if serve() never ran. (If it did,
	 * cpp-httplib already closed it on the way out, and joined its worker
	 * threads before listen_after_bind() returned.) */
	svr_->stop();
}

void Server::serve(bool handle_sigint)
{
	/* cpp-httplib closes the listening socket when the accept loop exits,
	 * so a server cannot be resumed. */
	if (served_)
		throw std::runtime_error("Tuber server cannot be restarted after it has stopped");
	served_ = true;

	/* A process-wide signal handler only belongs to the main thread
	 * (Python's signal module enforces the same rule), so callers serving
	 * from a background thread opt out. Go through PyOS_setsig, as Python
	 * does, and remember the previous disposition - normally Python's own
	 * handler - so it can be restored rather than clobbered. */
	PyOS_sighandler_t prev_sigint = SIG_DFL;
	if (handle_sigint) {
		sigint_target = this;
		prev_sigint = PyOS_setsig(SIGINT, &sigint);
	}

	bool ok;
	{
		py::gil_scoped_release release;
		ok = svr_->listen_after_bind();
	}

	if (handle_sigint) {
		PyOS_setsig(SIGINT, prev_sigint);
		sigint_target = nullptr;
	}

	/* A stop() that lands between bind and the start of the accept loop
	 * makes listen_after_bind() return false; that's a normal shutdown. */
	if (!ok && !stop_requested_)
		throw std::runtime_error("Tuber server could not listen on port " + std::to_string(port_));
}

void Server::stop()
{
	/* httplib::Server::stop() is safe to call concurrently with the accept
	 * loop, and svr_ never changes after construction, so this needs no
	 * further synchronization with serve(). */
	stop_requested_ = true;
	svr_->stop();
}


PYBIND11_MODULE(_tuber_runtime, m) {
	m.doc() = "Tuber server runtime library";

	py::class_<Server>(m, "Server",
	    "A webserver with a static webroot endpoint and a /tuber endpoint that\n"
	    "parses requests via a handler function.\n\n"
	    "Constructing a Server binds its port; call serve() to handle requests\n"
	    "until stop() is called (from another thread) or, when handling SIGINT,\n"
	    "the process is interrupted. A stopped server cannot be restarted.\n")
		.def(py::init<py::object, int, py::object, int>(),
		    "Arguments\n---------\n"
		    "handler : callable\n"
		    "    Callable that takes an encoded request bytestring plus request headers,\n"
		    "    and returns the response format and encoded response string. Signature:\n"
		    "    ``function(request: bytes, *, content_type: str, accept: str,\n"
		    "    x_tuber_options: str) -> tuple[str, str]``\n"
		    "    Absent headers are passed as empty strings.\n"
		    "port : int\n"
		    "    Port to bind. Port 0 selects any free port; see the ``port`` attribute.\n"
		    "webroot : str\n"
		    "    Location to serve static content\n"
		    "max_age : int\n"
		    "    Maximum cache residency for static (file) assets\n",
		    py::arg("handler"), py::arg("port")=80, py::arg("webroot")=py::none(), py::arg("max_age")=3600)
		.def_property_readonly("port", &Server::port,
		    "The port this server is bound to.")
		.def("serve", &Server::serve,
		    "Handle requests until stopped. Blocks the calling thread.\n\n"
		    "Arguments\n---------\n"
		    "handle_sigint : bool\n"
		    "    Install a SIGINT handler that stops the server for the duration of\n"
		    "    the call. Only appropriate from the main thread.\n",
		    py::arg("handle_sigint")=true)
		.def("stop", &Server::stop,
		    "Stop the server. Safe to call from any thread; serve() returns once the\n"
		    "server's worker threads have wound down.\n");
}
