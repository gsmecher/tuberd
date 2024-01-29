#include <iostream>
#include <csignal>
#include <filesystem>
#include <chrono>
#include <queue>
#include <boost/program_options.hpp>

#include <httpserver.hpp>
#include <httpserver/http_utils.hpp>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/embed.h>

#include <fmt/format.h>

namespace py = pybind11;
using namespace pybind11::literals;

using namespace httpserver;

namespace po = boost::program_options;
namespace fs = std::filesystem;

/* Formatter for Python objects */
template <typename T>
struct fmt::formatter<T, std::enable_if_t<std::is_base_of<py::object, T>::value, char>> : fmt::formatter<std::string> {

	template <typename FormatContext>
	auto format(py::object const& o, FormatContext& ctx) {
		return fmt::formatter<std::string>::format((std::string)py::str(o), ctx);
	}
};

using json_loads_t = std::function<py::object(std::string)>;
using json_dumps_t = std::function<std::string(py::object)>;

#define DLL_LOCAL __attribute__((visibility("hidden")))

/* Verbosity is expressed as a bit mask:
 *     0: none (default)
 *     1: report unexpected or unusual cases
 *     2: very noisy
 *     4: performance profiling
 */
static enum class Verbose {
	NONE = 0,	/* default */
	UNEXPECTED = 1,	/* report unexected or unusual cases */
	NOISY = 2,	/* message onslaught */
	TIMING = 4,
} verbose;

/* Operators for log levels */
inline constexpr int operator&(Verbose const& x, Verbose const& y) {
	return static_cast<int>(x) & static_cast<int>(y);
}

/* Needed to assign verbosity from program options. No validation occurs here. */
std::istream& operator>>(std::istream& in, Verbose& v) {
	int token;
	in >> token;
	v = static_cast<Verbose>(token);
	return in;
}

class timed_scope {
	public:
		timed_scope(std::string const& msg) : msg(msg) {
			if(verbose & Verbose::TIMING)
				t = std::chrono::steady_clock::now();
		}
		~timed_scope() {
			if(verbose & Verbose::TIMING) {
				std::chrono::duration dt = std::chrono::steady_clock::now() - t;
				fmt::print(stderr, "{}: {}ms\n",
						msg,
						std::chrono::duration_cast<std::chrono::milliseconds>(dt).count());
			}
		}
	private:
		std::string msg;
		std::chrono::time_point<std::chrono::steady_clock> t;
};

/* MIME types */
static const std::string MIME_JSON="application/json";
static const std::string MIME_DEFAULT="text/plain";

static const std::map<std::string, std::string> MIME_TYPES = {
	/* web content */
	{".css",   "text/css"},
	{".htm",   "text/html"},
	{".html",  "text/html"},
	{".js",    "text/javascript"},
	{".json",  MIME_JSON},
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

static inline py::object error_response(std::string const& msg) {
	return py::dict("error"_a=py::dict("message"_a=msg));
}

std::vector<std::string> warning_list;
static void showwarning(py::object message,
			py::object category,
			py::object filename,
			py::object lineno,
			py::object file,
			py::object line) {

	if(verbose & Verbose::NOISY)
		fmt::print(stderr, "... captured warning '{}'\n", py::str(message).cast<std::string>());

	warning_list.push_back(py::str(message).cast<std::string>());
}

static py::dict tuber_server_invoke(py::dict &registry,
		py::dict const& call,
		json_loads_t const& json_loads,
		json_dumps_t const& json_dumps) {

	timed_scope ts(__func__);

	/* Fast path: function calls */
	if(call.contains("object") && call.contains("method")) {

		std::string oname = call["object"].cast<std::string>();
		std::string mname = call["method"].cast<std::string>();

		/* Populate python_args */
		py::list python_args;
		if(call.contains("args")) {
			try {
				python_args = call["args"];
			} catch(py::error_already_set const&) {
				return error_response("'args' wasn't an array.");
			}
		}

		/* Populate python_kwargs */
		py::dict python_kwargs;
		if(call.contains("kwargs")) {
			try {
				python_kwargs = call["kwargs"];
			} catch(py::error_already_set const&) {
				return error_response("'kwargs' wasn't an object.");
			}
		}

		/* Look up object */
		py::object o = registry[oname.c_str()];
		if(!o)
			return error_response("Object not found in registry.");

		/* Look up method */
		py::object m = o.attr(mname.c_str());
		if(!m)
			return error_response("Method not found in object.");

		if(verbose & Verbose::NOISY)
			fmt::print(stderr, "Dispatch: {}::{}(*{}, **{})...\n",
					oname, mname,
					python_args,
					python_kwargs);

		/* Dispatch to Python - failures emerge as exceptions */
		timed_scope ts("Python dispatch");
		py::object response = py::none();
		try {
			response = py::dict("result"_a=m(*python_args, **python_kwargs));
		} catch(std::exception &e) {
			response = error_response(e.what());
		}

		/* Capture warnings, if any */
		if(!warning_list.empty()) {
			response["warnings"] = warning_list;
			warning_list.clear();
		}

		if(verbose & Verbose::NOISY)
			fmt::print(stderr, "... response was {}\n", json_dumps(response));

		return response;
	}

	if(verbose & Verbose::NOISY)
		fmt::print(stderr, "Delegating json {} to describe() slowpath.\n", call);

	/* Slow path: object metadata, properties */
	return py::eval("describe")(registry, call);
}

/* Responder for tuber resources exported via JSON.
 *
 * This code serves both "hot" (method call) and "cold" paths (metadata, cached
 * property fetches). Hot paths are coded in c++. Cold paths are coded in
 * Python (in the preamble). */
class DLL_LOCAL tuber_resource : public http_resource {
	public:
		tuber_resource(py::dict const& reg,
				json_loads_t json_loads,
				json_dumps_t json_dumps) :
			reg(reg),
			json_loads(json_loads),
			json_dumps(json_dumps) {};

		const std::shared_ptr<http_response> render(const http_request& req) {
			/* Acquire the GIL. This makes us thread-safe -
			 * but any methods we invoke should release the
			 * GIL (especially if they do their own
			 * threaded things) in order to avoid pile-ups.
			 */
			py::gil_scoped_acquire acquire;

			try {
				if(verbose & Verbose::NOISY)
					fmt::print(stderr, "Request: {}\n", req.get_content());

				/* Parse JSON */
				py::object request_obj = json_loads(req.get_content());

				if(py::isinstance<py::dict>(request_obj)) {
					/* Simple JSON object - invoke it and return the results. */
					py::object result;
					try {
						result = tuber_server_invoke(reg, request_obj, json_loads, json_dumps);
					} catch(std::exception &e) {
						result = error_response(e.what());
						if(verbose & Verbose::NOISY)
							fmt::print("Exception path response: {}\n", (std::string)(py::str)json_dumps(result));
					}
					return std::shared_ptr<http_response>(new string_response(json_dumps(result), http::http_utils::http_ok, MIME_JSON));

				} else if(py::isinstance<py::list>(request_obj)) {
					py::list request_list = request_obj;

					/* Array of sub-requests. Error-handling semantics are
					 * embedded here: if something goes wrong, we do not
					 * execute subsequent calls but /do/ pad the results
					 * list to have the expected size. */
					py::list result(py::len(request_list));

					bool early_bail = false;
					for(size_t i=0; i<result.size(); i++) {
						/* If something went wrong earlier in the loop, don't execute anything else. */
						if(early_bail) {
							result[i] = error_response("Something went wrong in a preceding call.");
							continue;
						}

						try {
							result[i] = tuber_server_invoke(reg, request_list[i], json_loads, json_dumps);
						} catch(std::exception &e) {
							/* Indicates an internal error - this does not normally happen */
							result[i] = error_response(e.what());
							early_bail = true;
						}

						if(result[i].contains("error")) {
							/* Indicates client code flagged an error - this is a nominal code path */
							early_bail = true;
						}
					}

					timed_scope ts("Happy-path JSON serialization");

					/* FIXME: serialization failure in an array call returns with an object structure! */
					std::string result_json = json_dumps(result);
					return std::shared_ptr<http_response>(new string_response(result_json, http::http_utils::http_ok, MIME_JSON));
				}
				else {
					std::string error = json_dumps(error_response("Unexpected type in request."));
					return std::shared_ptr<http_response>(new string_response(error, http::http_utils::http_ok, MIME_JSON));
				}
			} catch(std::exception const& e) {
				if(verbose & Verbose::UNEXPECTED)
					fmt::print(stderr, "Unhappy-path response {}\n", e.what());

				std::string error = json_dumps(error_response(e.what()));
				return std::shared_ptr<http_response>(new string_response(error, http::http_utils::http_ok, MIME_JSON));
			}
		}
	private:
		py::dict reg;
		json_loads_t json_loads;
		json_dumps_t json_dumps;
};

/* Responder for files served out of the local filesystem.
 *
 * This code is NOT part of the "hot" path, so simplicity is more important
 * than performance.
 */
class DLL_LOCAL file_resource : public http_resource {
	public:
		file_resource(fs::path webroot, int max_age) : webroot(webroot), max_age(max_age) {};

		const std::shared_ptr<http_response> render_GET(const http_request& req) {
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
			if(!fs::is_regular_file(path)) {
				if(verbose & Verbose::UNEXPECTED)
					fmt::print(stderr, "Unable or unwilling to serve missing or non-file resource {}\n", path.string());

				return std::shared_ptr<http_response>(new string_response("No such file or directory.\n", http::http_utils::http_not_found));
			}

			/* Figure out a MIME type to use */
			std::string mime_type = MIME_DEFAULT;
			auto it = MIME_TYPES.find(path.extension().string());
			if(it != MIME_TYPES.end())
				mime_type = it->second;

			if(verbose & Verbose::NOISY)
				fmt::print(stderr, "Serving {} with {} using MIME type {}\n", req.get_path(), path.string(), mime_type);

			/* Construct response and return it */
			auto response = std::shared_ptr<file_response>(new file_response(path.string(), http::http_utils::http_ok, mime_type));
			response->with_header(http::http_utils::http_header_cache_control, fmt::format("max-age={}", max_age));
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

int main(int argc, char **argv) {
	/*
	 * Parse command-line arguments
	 */

	int port;
	int max_age;
	bool orjson_with_numpy=false;
	std::string preamble, registry, webroot;
	std::string json_module;

	po::options_description desc("tuberd");
	desc.add_options()
		("help,h", "produce help message")

		("max-age",
		 po::value<int>(&max_age)->default_value(3600),
		 "maximum cache residency for static (file) assets")

		("json,j",
		 po::value<std::string>(&json_module)->default_value("json"),
		 "Python JSON module to use for serialization/deserialization")

		("orjson-with-numpy",
		 po::bool_switch(&orjson_with_numpy)->default_value(false),
		 "Use ORJSON module with fast NumPy serialization support")

		("port,p",
		 po::value<int>(&port)->default_value(80),
		 "port")

		("preamble",
		 po::value<std::string>(&preamble)->default_value("/usr/share/tuberd/preamble.py"),
		 "location of slow-path Python code")

		("registry",
		 po::value<std::string>(&registry)->default_value("/usr/share/tuberd/registry.py"),
		 "location of registry Python code")

		("webroot,w",
		 po::value<std::string>(&webroot)->default_value("/var/www/"),
		 "location to serve static content")

		("verbose,v",
		 po::value<Verbose>(&verbose),
		 "verbosity (default: 0)")

		;

	po::variables_map vm;
	po::store(po::parse_command_line(argc, argv, desc), vm);
	po::notify(vm);

	if(vm.count("help")) {
		std::cout << desc << std::endl;
		return 1;
	}

	/*
	 * Initialize Python runtime
	 */

	py::scoped_interpreter python;

	/* By default, capture warnings */
	py::module warnings = py::module::import("warnings");
	warnings.attr("showwarning") = py::cpp_function(showwarning);

	/* Learn how the Python half lives */
	try {
		py::eval_file(preamble);
	} catch(std::exception const& e) {
		fmt::print(stderr, "Error executing preamble {}!\n({})\n", preamble, e.what());
		return 2;
	}

	/* Load indicated Python initialization scripts */
	try {
		py::eval_file(registry);
	} catch(std::exception const& e) {
		fmt::print(stderr, "Error executing registry {}!\n({})\n", registry, e.what());
		return 3;
	}

	/* Import JSON dumps function so we can use it */
	json_loads_t json_loads;
	json_dumps_t json_dumps;
	try {
		if(orjson_with_numpy)
			json_module = "orjson";

		/* Import Python loads/dumps */
		py::module json = py::module::import(json_module.c_str());
		py::object py_loads = json.attr("loads");
		py::object py_dumps = json.attr("dumps");

		json_loads = [py_loads](std::string s) { return py_loads(s); };
		json_dumps = [py_dumps](py::object o) { return py_dumps(o).cast<std::string>(); };

		/* If using orjson with NumPy, overload dumps with the right magic. */
		if(orjson_with_numpy) {
			py::object OPT_SERIALIZE_NUMPY = json.attr("OPT_SERIALIZE_NUMPY");
			json_dumps = [py_dumps, OPT_SERIALIZE_NUMPY](py::object o) {
				return py_dumps(o, std::nullopt, OPT_SERIALIZE_NUMPY).cast<std::string>();
			};
		}
	} catch(std::exception const& e) {
		fmt::print(stderr, "Unable to import loads/dumps from module {} ({})\n",
				json_module, e.what());
		return 4;
	}

	/* Create a registry */
	py::dict reg = py::eval("registry");

	py::gil_scoped_release release;

	/*
	 * Start webserver
	 */

	std::unique_ptr<http_resource> fr = nullptr;
	std::unique_ptr<http_resource> tr = nullptr;
	ws = std::make_unique<webserver>(create_webserver(port).start_method(http::http_utils::THREAD_PER_CONNECTION));

	std::signal(SIGINT, &sigint);

	/* Set up /tuber endpoint */
	tr = std::make_unique<tuber_resource>(reg, json_loads, json_dumps);
	tr->disallow_all();
	tr->set_allowing(MHD_HTTP_METHOD_POST, true);
	ws->register_resource("/tuber", tr.get());

	/* If a valid webroot was provided, serve static content for other paths. */
	try {
		fr = std::make_unique<file_resource>(fs::canonical(webroot), max_age);
		fr->disallow_all();
		fr->set_allowing(MHD_HTTP_METHOD_GET, true);
		ws->register_resource("/", fr.get(), true);
	} catch(fs::filesystem_error const& e) {
		fmt::print(stderr, "Unable to resolve webroot {}; not serving static content.\n", webroot);
	}

	/* Go! */
	try {
		ws->start(true);
	} catch(std::exception const& e) {
		fmt::print("Error: {}\n", e.what());
	}

	return 0;
}
