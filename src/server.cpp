#include <csignal>
#include <filesystem>
#include <iostream>
#include <map>
#include <sstream>
#include <string>

#include <httpserver.hpp>
#include <httpserver/http_utils.hpp>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;
using namespace pybind11::literals;

using namespace httpserver;

namespace fs = std::filesystem;

/* pybind11 assumes we're building a shared library (as would be normal for a
 * module, rather than an embedded interpreter) and attempts to tweak
 * shared-library symbol visibility as a result. Even though we don't care
 * about symbol visibility here, we follow its lead to squash warnings. */
#define DLL_LOCAL __attribute__((visibility("hidden")))

/* MIME types */
static const std::string MIME_JSON="application/json";
static const std::string MIME_CBOR="application/cbor";
static const std::string MIME_DEFAULT="text/plain";

static const std::map<std::string, std::string> MIME_TYPES = {
	/* web content */
	{".css",   "text/css"},
	{".htm",   "text/html"},
	{".html",  "text/html"},
	{".js",    "text/javascript"},
	{".json",  MIME_JSON},
	{".cbor",  MIME_CBOR},
	/* No entry for .txt needed - it's the fallback case */

	/* fonts */
	{".eot",   "application/vnd.ms-fontobject"},
	{".ttf",   "font/ttf"},
	{".woff",  "font/woff"},
	{".woff2", "font/woff2"},

	/* images */
	{".gif",   "image/gif"},
	{".ico",   "image/vnd.microsoft.icon"},
	{".jpeg",  "image/jpeg"},
	{".jpg",   "image/jpeg"},
	{".png",   "image/png"},
	{".svg",   "image/svg+xml"},

	/* application specific */
	{".pdf",   "application/pdf"},
};

/* Responder for tuber resources exported via JSON.
 *
 * This code serves both "hot" (method call) and "cold" paths (metadata, cached
 * property fetches). All paths are coded in Python (in the tuber.server package),
 * with hot path dispatch to C++ handled by the user. */
class DLL_LOCAL tuber_resource : public http_resource {
	public:
		tuber_resource(py::object const& handler) : handler(handler) {};

		std::shared_ptr<http_response> render(const http_request& req) {
			/* Acquire the GIL. This makes us thread-safe -
			 * but any methods we invoke should release the
			 * GIL (especially if they do their own
			 * threaded things) in order to avoid pile-ups.
			 */
			py::gil_scoped_acquire acquire;

			py::tuple resp = handler(req.get_content(), req.get_headers());

			std::string responseFormat = resp[0].cast<std::string>();
			std::string response = resp[1].cast<std::string>();

			return std::make_shared<string_response>(response, http::http_utils::http_ok, responseFormat);
		}
	private:
		py::object handler;
};

/* Responder for files served out of the local filesystem.
 *
 * This code is NOT part of the "hot" path, so simplicity is more important
 * than performance.
 */
class DLL_LOCAL file_resource : public http_resource {
	public:
		file_resource(fs::path webroot, int max_age) : webroot(webroot), max_age(max_age) {};

		std::shared_ptr<http_response> render_GET(const http_request& req) {
			/* Start with webroot and append path segments from
			 * HTTP request.
			 *
			 * Dot segments ("..") are resolved before we are called -
			 * hence a path traversal out of webroot seems
			 * impossible, provided we are careful about following
			 * links.  (If this matters to you, cross-check it
			 * yourself.) */
			auto path = webroot;
			for(auto &p : req.get_path_pieces())
				path.append(p);

			/* Append index.html when a directory is requested */
			if(fs::is_directory(path) && fs::is_regular_file(path / "index.html"))
				path /= "index.html";

			/* Serve 404 if the resource does not exist, or we couldn't find it */
			if(!fs::is_regular_file(path))
				return std::make_shared<string_response>("No such file or directory.\n", http::http_utils::http_not_found);

			/* Figure out a MIME type to use */
			std::string mime_type = MIME_DEFAULT;
			auto it = MIME_TYPES.find(path.extension().string());
			if(it != MIME_TYPES.end())
				mime_type = it->second;

			/* Construct response and return it */
			auto response = std::make_shared<file_response>(path.string(), http::http_utils::http_ok, mime_type);
			response->with_header(http::http_utils::http_header_cache_control, 
			                      "max-age="+std::to_string(max_age));
			return response;
		}
	private:
		fs::path webroot;
		int max_age;
};

/* Unfortunately, we need to carry around a global pointer just for signal handling. */
static std::unique_ptr<webserver> ws = nullptr;
static void sigint(int signo) {
	if(ws)
		ws->stop();
}

static void run_server(py::object handler, int port=80, py::object webroot=py::none(), int max_age=3600)
{
	/* Can only run one server at a time */
	if (ws)
		throw std::runtime_error("Tuber server already running!");

	/*
	 * Start webserver
	 */

	std::unique_ptr<http_resource> fr = nullptr;
	std::unique_ptr<http_resource> tr = nullptr;
	ws = std::make_unique<webserver>(create_webserver(port).start_method(http::http_utils::THREAD_PER_CONNECTION));

	std::signal(SIGINT, &sigint);

	/* Set up /tuber endpoint */
	tr = std::make_unique<tuber_resource>(handler);
	tr->disallow_all();
	tr->set_allowing(MHD_HTTP_METHOD_POST, true);
	ws->register_resource("/tuber", tr.get());

	py::gil_scoped_release release;

	/* If a valid webroot was provided, serve static content for other paths. */

	if (!webroot.is_none()) {
		std::string wroot = webroot.cast<std::string>();
		fr = std::make_unique<file_resource>(fs::canonical(wroot), max_age);
		fr->disallow_all();
		fr->set_allowing(MHD_HTTP_METHOD_GET, true);
		ws->register_resource("/", fr.get(), true);
	}

	/* Go! */
	ws->start(true);
}


PYBIND11_MODULE(_tuber_runtime, m) {
	m.doc() = "Tuber server runtime library";

	m.def("run_server", &run_server,
	    "Main server runtime function that creates a webserver with a static webroot\n"
	    "endpoint and a /tuber endpoint that parses requests via a handler function,\n"
	    "and runs the server until an interrupt is signaled.\n\n"
	    "Arguments\n---------\n"
	    "handler : callable\n"
	    "    Callable that takes an encoded request string and header dictionary arguments,\n"
	    "    and returns the response format and encoded response string.  Signature:\n"
	    "    ``function(request: str, headers: dict) -> tuple[str, str]``\n"
	    "port : int\n"
	    "    Port on which to run the server\n"
	    "webroot : str\n"
	    "    Location to serve static content\n"
	    "max_age : int\n"
	    "    Maximum cache residency for static (file) assets\n",
	    py::arg("handler"), py::arg("port")=80, py::arg("webroot")=py::none(), py::arg("max_age")=3600);
}
