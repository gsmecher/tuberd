#include <chrono>
#include <csignal>
#include <filesystem>
#include <iostream>
#include <map>
#include <queue>
#include <sstream>
#include <string>
#include <vector>

#include <getopt.h>

#include <httpserver.hpp>
#include <httpserver/http_utils.hpp>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/embed.h>

#include <fmt/format.h>

namespace py = pybind11;
using namespace pybind11::literals;

using namespace httpserver;

namespace fs = std::filesystem;

/* Formatter for Python objects */
template <typename T>
struct fmt::formatter<T, std::enable_if_t<std::is_base_of<py::object, T>::value, char>> : fmt::formatter<std::string> {

	template <typename FormatContext>
	auto format(py::object const& o, FormatContext& ctx) {
		return fmt::formatter<std::string>::format((std::string)py::str(o), ctx);
	}
};

/* pybind11 assumes we're building a shared library (as would be normal for a
 * module, rather than an embedded interpreter) and attempts to tweak
 * shared-library symbol visibility as a result. Even though we don't care
 * about symbol visibility here, we follow its lead to squash warnings. */
#define DLL_LOCAL __attribute__((visibility("hidden")))

struct DLL_LOCAL Codec {
	using loads_t = std::function<py::object(std::string)>;
	using dumps_t = std::function<std::string(py::object)>;

	loads_t loads;
	dumps_t dumps;
};

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

static std::vector<std::string> parseCommaSepList(std::string_view rawl){
	//this makes no special effort to be efficient; it just copies strings all over
	std::istringstream is(std::string{rawl});
	std::vector<std::string> results;
	std::string item;
	while(is >> item){
		if(item.empty())
			continue;
		else if(item.back()==',')
			results.push_back(item.substr(0,item.size()-1));
		else
			results.push_back(item);
	}
	return results;
}

static py::dict tuber_server_invoke(py::dict &registry,
		py::dict const& call,
		Codec::loads_t const& loads,
		Codec::dumps_t const& dumps) {

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
			fmt::print(stderr, "... response was {}\n", dumps(response));

		return response;
	}

	if(verbose & Verbose::NOISY)
		fmt::print(stderr, "Delegating json {} to describe() slowpath.\n", call);

	/* Slow path: object metadata, properties */
	return py::eval("tuber.server.describe")(registry, call);
}

/* Responder for tuber resources exported via JSON.
 *
 * This code serves both "hot" (method call) and "cold" paths (metadata, cached
 * property fetches). Hot paths are coded in c++. Cold paths are coded in
 * Python (in the preamble). */
class DLL_LOCAL tuber_resource : public http_resource {
	public:
		using CodecMap = std::map<std::string, Codec>;
		tuber_resource(py::dict const& reg,
				const CodecMap& codecs) :
			reg(reg),
			codecs(codecs) {};

		std::string determineResponseFormat(std::string_view accept, std::string_view requestFormat) {
			if (accept.empty()) //If nothing specified, use what the client used
				return std::string(requestFormat);
			auto acceptedV = parseCommaSepList(accept);
			for (const auto& accepted : acceptedV) {
				if (codecs.count(accepted))
					return std::string(accepted);
			}
			// If the client claims to accept anything, pick arbitrarily
			if(std::find(acceptedV.begin(),acceptedV.end(),"*/*")!=acceptedV.end()
			   || std::find(acceptedV.begin(),acceptedV.end(),"application/*")!=acceptedV.end())
				return codecs.cbegin()->first;
			throw std::runtime_error(fmt::format("Not able to encode any media type matching {}", accept));
		}

		std::shared_ptr<http_response> render(const http_request& req) {
			/* Acquire the GIL. This makes us thread-safe -
			 * but any methods we invoke should release the
			 * GIL (especially if they do their own
			 * threaded things) in order to avoid pile-ups.
			 */
			py::gil_scoped_acquire acquire;

			CodecMap::const_iterator responseCodecIt;
			std::string responseFormat;

			// We assume that this cannot fail because JSON must always be available
			auto setDefaultFormat = [&](){
				responseFormat = MIME_JSON;
				responseCodecIt = codecs.find(responseFormat);
			};
			setDefaultFormat();
			auto encodeResponse = [&responseCodecIt](py::object value){
				return responseCodecIt->second.dumps(value);
			};

			try {
				if(verbose & Verbose::NOISY)
					fmt::print(stderr, "Request: {}\n", req.get_content());

				/* Figure out formats for request and response */
				std::string requestFormat = std::string(req.get_header("Content-Type"));
				if (requestFormat.empty()) // assume JSON if unspecified
					requestFormat = MIME_JSON;
				auto requestCodecIt = codecs.find(requestFormat);
				if (requestCodecIt == codecs.end()){
					std::string message = fmt::format("Not able to decode media type {}", requestFormat);
					if(verbose & Verbose::NOISY)
						fmt::print("Exception path response: {}\n", message);
					return std::make_shared<string_response>(encodeResponse(error_response(message)), http::http_utils::http_ok, responseFormat);
				}

				responseFormat = determineResponseFormat(req.get_header("Accept"), requestFormat);
				responseCodecIt = codecs.find(responseFormat);
				if (responseCodecIt == codecs.end()){
					std::string message = fmt::format("Not able to encode media type {}", responseFormat);
					if(verbose & Verbose::NOISY)
						fmt::print("Exception path response: {}\n", message);
					setDefaultFormat();
					return std::make_shared<string_response>(encodeResponse(error_response(message)), http::http_utils::http_ok, responseFormat);
				}

				auto xopts = parseCommaSepList(req.get_header("X-Tuber-Options"));
				bool continue_on_error = false;
				if (xopts.size() > 0) {
					continue_on_error = std::find(xopts.begin(), xopts.end(), "continue-on-error") != xopts.end();
				}

				/* Parse request */
				std::string content(req.get_content());
				py::object request_obj = requestCodecIt->second.loads(content);

				if(py::isinstance<py::dict>(request_obj)) {
					/* Simple JSON object - invoke it and return the results. */
					py::object result;
					try {
						result = tuber_server_invoke(reg, request_obj, responseCodecIt->second.loads, responseCodecIt->second.dumps);
					} catch(std::exception &e) {
						result = error_response(e.what());
						if(verbose & Verbose::NOISY)
							fmt::print("Exception path response: {}\n", e.what());
					}
					return std::shared_ptr<http_response>(new string_response(encodeResponse(result), http::http_utils::http_ok, responseFormat));

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
							result[i] = tuber_server_invoke(reg, request_list[i], responseCodecIt->second.loads, responseCodecIt->second.dumps);
						} catch(std::exception &e) {
							/* Indicates an internal error - this does not normally happen */
							result[i] = error_response(e.what());
							if (!continue_on_error)
								early_bail = true;
						}

						if(result[i].contains("error") && !continue_on_error) {
							/* Indicates client code flagged an error - this is a nominal code path */
							early_bail = true;
						}
					}

					timed_scope ts("Happy-path serialization");

					/* FIXME: serialization failure in an array call returns with an object structure! */
					std::string encoded_result = encodeResponse(result);
					return std::shared_ptr<http_response>(new string_response(encoded_result, http::http_utils::http_ok, responseFormat));
				}
				else {
					std::string error = encodeResponse(error_response("Unexpected type in request."));
					return std::shared_ptr<http_response>(new string_response(error, http::http_utils::http_ok, responseFormat));
				}
			} catch(std::exception const& e) {
				if(verbose & Verbose::UNEXPECTED)
					fmt::print(stderr, "Unhappy-path response {}\n", e.what());

				std::string error = encodeResponse(error_response(e.what()));
				return std::shared_ptr<http_response>(new string_response(error, http::http_utils::http_ok, responseFormat));
			}
		}
	private:
		py::dict reg;
		const CodecMap& codecs;
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

/* pretty print CLI options */
#define PRINTOPT(o, h) \
  fmt::print("  {}\n      {}\n", o, h)
#define PRINTOPT2(o, h, d) \
  fmt::print("  {}\n      {} (default: {})\n", o, h, d)

int main(int argc, char **argv) {
	/*
	 * Parse command-line arguments
	 */

	int port = 80;
	int max_age = 3600;
	int orjson_with_numpy = 0;
	std::string preamble = "/usr/share/tuberd/preamble.py";
	std::string registry = "/usr/share/tuberd/registry.py";
	std::string webroot = "/var/www/";
	std::string json_module = "json";

	const option long_opts[] = {
		{"orjson-with-numpy", no_argument, &orjson_with_numpy, 1},
		{"max-age", required_argument, nullptr, 'a'},
		{"json", required_argument, nullptr, 'j'},
		{"port", required_argument, nullptr, 'p'},
		{"preamble", required_argument, nullptr, 'm'},
		{"registry", required_argument, nullptr, 'r'},
		{"webroot", required_argument, nullptr, 'w'},
		{"verbose", required_argument, nullptr, 'v'},
		{"help", no_argument, nullptr, 'h'},
		{nullptr, no_argument, nullptr, 0}
	};

	while (true) {
		const auto c = getopt_long(argc, argv, "j:p:w:v:h", long_opts, nullptr);
		if (c == -1)
			break;

		switch (c) {
		case 0:
			break;
		case 'a':
			max_age = std::stoi(optarg);
			break;
		case 'j':
			json_module = std::string(optarg);
			break;
		case 'p':
			port = std::stoi(optarg);
			break;
		case 'r':
			registry = std::string(optarg);
			break;
		case 'w':
			webroot = std::string(optarg);
			break;
		case 'v':
			verbose = static_cast<Verbose>(std::stoi(optarg));
			break;
		case 'h':
		case '?':
		default:
			fmt::print("Usage: {} [options]\n\nOptions:\n", argv[0]);
			PRINTOPT("-h [ --help ]", "produce help message");
			PRINTOPT2("--max-age N",
			    "maximum cache residency for static (file) assets", max_age);
			PRINTOPT2("-j [ --json ] NAME",
			    "Python JSON module to use for serialization/deserialization",
			    json_module);
			PRINTOPT("--orjson-with-numpy",
			    "use ORJSON module with fast NumPy serialization support");
			PRINTOPT2("-p [ --port ] PORT", "port", port);
			PRINTOPT2("--registry PATH", "location of registry Python code",
			    registry);
			PRINTOPT2("-w [ --webroot ] PATH",
			    "location to serve static content", webroot);
			PRINTOPT2("-v [ --verbose ] N", "verbosity", (int)verbose);
			return 1;
		}
	}

	/*
	 * Initialize Python runtime
	 */

	/* Indicate to anyone who cares that we're running server-side */
	setenv("TUBER_SERVER", "1", 1);
	py::scoped_interpreter python;

	/* The following fixups need these */
	py::exec("import sys, os, sysconfig");

	/* Add cwd to PYTHONPATH */
	py::exec("sys.path.append('.');");

	/* Ensure site.ENABLE_USER_SITE. This is a backwards compatibility
	 * thing; current pybind11 doesn't need it. */
	py::exec("sys.path.append("
		"os.path.expanduser("
			"'~/.local/lib/python{ver}/site-packages'"
				".format(ver=sysconfig.get_python_version())))");

	/* By default, capture warnings */
	py::module warnings = py::module::import("warnings");
	warnings.attr("showwarning") = py::cpp_function(showwarning);

	/* Learn how the Python half lives */
	try {
		py::exec("import tuber.server");
	} catch(std::exception const& e) {
		fmt::print("Failed to import tuber.server!");
		return 2;
	}

	/* Load indicated Python initialization scripts */
	try {
		py::eval_file(registry);
	} catch(std::exception const& e) {
		fmt::print(stderr, "Error executing registry {}!\n({})\n", registry, e.what());
		return 3;
	}

	std::map<std::string, Codec> codecs;
	py::dict py_codecs;

	try {
		py_codecs = py::eval("tuber.server.Codecs");
	} catch(std::exception const& e) {
		fmt::print(stderr, "Unable to import server codecs ({})\n", e.what());
		return 4;
	}

	/* Import JSON dumps function so we can use it */
	try {
		if(orjson_with_numpy)
			json_module = "orjson";

		/* Import Python loads/dumps */
		py::object py_codec = py_codecs[json_module.c_str()];
		py::object py_loads = py_codec.attr("decode");
		py::object py_dumps = py_codec.attr("encode");

		Codec::loads_t json_loads = [py_loads](std::string s) { return py_loads(s); };
		Codec::dumps_t json_dumps = [py_dumps](py::object o) {
			return py_dumps(o).cast<std::string>();
		};
		codecs.emplace(MIME_JSON, Codec{json_loads, json_dumps});
	} catch(std::exception const& e) {
		fmt::print(stderr, "Unable to import {} codec ({})\n", json_module, e.what());
		return 4;
	}

	try{
		py::object py_codec = py_codecs["cbor"];
		py::object py_loads = py_codec.attr("decode");
		py::object py_dumps = py_codec.attr("encode");

		Codec::loads_t cbor_loads = [py_loads](std::string s) { return py_loads(s); };
		Codec::dumps_t cbor_dumps = [py_dumps](py::object o) {
			return py_dumps(o).cast<std::string>();
		};
		codecs.emplace(MIME_CBOR, Codec{cbor_loads, cbor_dumps});
	} catch(std::exception const& e) {
		if(verbose & Verbose::NOISY)
			fmt::print(stderr, "Could not import cbor2, CBOR will not be available: {}\n", e.what());
	}

	/* Create a registry */
	py::dict reg = py::eval("registry");

	/*
	 * Start webserver
	 */

	std::unique_ptr<http_resource> fr = nullptr;
	std::unique_ptr<http_resource> tr = nullptr;
	ws = std::make_unique<webserver>(create_webserver(port).start_method(http::http_utils::THREAD_PER_CONNECTION));

	std::signal(SIGINT, &sigint);

	/* Set up /tuber endpoint */
	tr = std::make_unique<tuber_resource>(reg, codecs);
	tr->disallow_all();
	tr->set_allowing(MHD_HTTP_METHOD_POST, true);
	ws->register_resource("/tuber", tr.get());

	py::gil_scoped_release release;

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
