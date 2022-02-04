#include <iostream>
#include <csignal>
#include <filesystem>
#include <boost/program_options.hpp>

#include <httpserver.hpp>
#include <httpserver/http_utils.hpp>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/embed.h>
#include <pybind11_json/pybind11_json.hpp>

#include <fmt/format.h>

#include <nlohmann/json.hpp>

namespace py = pybind11;
using namespace pybind11::literals;
using json = nlohmann::json;
using namespace httpserver;
namespace po = boost::program_options;
namespace fs = std::filesystem;

#define DLL_LOCAL __attribute__((visibility("hidden")))

/* Verbosity:
 *     0: none (default)
 *     1: report unexpected or unusual cases
 *     2: very noisy
 */
static enum class Verbose {
	NONE = 0,	/* default */
	UNEXPECTED = 1,	/* report unexected or unusual cases */
	NOISY = 2,	/* message onslaught */
} verbose;

/* Log levels stack up; comparing them is useful when generating messages */
int operator<=>(Verbose const& v1, Verbose const& v2) {
	return static_cast<int>(v1) - static_cast<int>(v2);
}

/* Needed to assign verbosity from program options. */
std::istream& operator>>(std::istream& in, Verbose& v) {
	int token;
	in >> token;

	switch(token) {
		case 0:
			v = Verbose::NONE;
			break;
		case 1:
			v = Verbose::UNEXPECTED;
			break;
		case 2:
			v = Verbose::NOISY;
			break;
		default:
			in.setstate(std::ios_base::failbit);
	}
	return in;
}

/* MIME types */
static const std::string MIME_JSON="application/json";
static const std::map<std::string, std::string> MIME_TYPES = {
	{".css",  "text/css"},
	{".gif",  "image/gif"},
	{".htm",  "text/html"},
	{".html", "text/html"},
	{".jpeg", "image/jpeg"},
	{".jpg",  "image/jpeg"},
	{".js",   "application/javascript"},
	{".json", MIME_JSON},
	{".pdf",  "application/pdf"},
	{".png",  "image/png"},
	{".svg",  "image/svg+xml"},
	{".ttf",  "application/font-sfnt"},
	{".woff", "application/font-woff"},
};

static json error_response(std::string const& msg) {
	return {{ "error", {{ "message", msg }}, }};
}

static json tuber_server_invoke(py::dict &registry, const json &call) {
	/* Acquire the GIL. This makes us thread-safe - but any methods we
	 * invoke should release the GIL (especially if they do their own
	 * threaded things) in order to avoid pile-ups. */
	py::gil_scoped_acquire acquire;

	/* Fast path: function calls */
	if(call.contains("object") && call.contains("method")) {

		std::string oname = call["object"];
		std::string mname = call["method"];

		/* Populate python_args */
		py::tuple python_args;
		auto it_args = call.find("args");
		if(it_args != call.end()) {
			if(!it_args->is_array())
				return error_response("'args' wasn't an array.");

			python_args = *it_args;
		}

		/* Populate python_kwargs */
		py::dict python_kwargs;
		auto it_kwargs = call.find("kwargs");
		if(it_kwargs != call.end()) {
			if(!it_kwargs->is_object())
				return error_response("'kwargs' wasn't an object.");
			python_kwargs = *it_kwargs;
		}

		/* Look up object */
		py::object o = registry[oname.c_str()];
		if(!o)
			return error_response("Object not found in registry.");

		/* Look up method */
		py::object m = o.attr(mname.c_str());
		if(!m)
			return error_response("Method not found in object.");

		if(verbose >= Verbose::NOISY)
			fmt::print(stderr, "Dispatch: {}::{}(*{}, **{})...\n",
					oname, mname,
					json(python_args).dump(),
					json(python_kwargs).dump());

		/* Dispatch to Python */
		py::object response = m(*python_args, **python_kwargs);

		if(verbose >= Verbose::NOISY)
			fmt::print(stderr, "... response was {}\n", json(response).dump());

		/* Cast back to JSON, wrap in a result object, and return */
		return { { "result", response } };
	}

	if(verbose >= Verbose::NOISY)
		fmt::print(stderr, "Delegating json {} to describe() slowpath.\n", call.dump());

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
		tuber_resource(py::dict const& reg) : reg(reg) {};

		const std::shared_ptr<http_response> render(const http_request& req) {
			try {
				if(verbose >= Verbose::NOISY)
					fmt::print(stderr, "Request: {}\n", req.get_content());

				/* Parse JSON */
				json request_body_json = json::parse(req.get_content());

				if(request_body_json.is_object()) {
					/* Simple JSON object - invoke it and return the results. */
					json result_json;
					try {
						result_json = tuber_server_invoke(reg, request_body_json);
					} catch(std::exception &e) {
						result_json = error_response(e.what());
						if(verbose >= Verbose::NOISY)
							fmt::print("Exception path response: {}\n", result_json.dump());
					}
					return std::shared_ptr<http_response>(new string_response(result_json.dump(), http::http_utils::http_ok, MIME_JSON));

				} else if(request_body_json.is_array()) {
					/* Array of sub-requests. Error-handling semantics are
					 * embedded here: if something goes wrong, we do not
					 * execute subsequent calls but /do/ pad the results
					 * list to have the expected size. */
					std::vector<json> result(request_body_json.size());

					size_t i;
					try {
						for(i=0; i<result.size(); i++)
							result[i] = tuber_server_invoke(reg, request_body_json.at(i));
					} catch(std::exception &e) {
						result[i] = error_response(e.what());
						if(verbose >= Verbose::NOISY)
							fmt::print("Exception path response: {}\n", result[i].dump());
						for(i++; i<result.size(); i++)
							result.at(i) = error_response("Something went wrong in a preceding call.");
					}

					json result_json = result;
					return std::shared_ptr<http_response>(new string_response(result_json.dump(), http::http_utils::http_ok, MIME_JSON));
				}
				else {
					json result_json = error_response("Unexpected type in request.");
					return std::shared_ptr<http_response>(new string_response(result_json.dump(), http::http_utils::http_ok, MIME_JSON));
				}
			} catch(std::exception &e) {
				if(verbose >= Verbose::UNEXPECTED)
					fmt::print(stderr, "Unhappy-path response {}\n", e.what());

				json response = error_response(e.what());
				return std::shared_ptr<http_response>(new string_response(response.dump(), http::http_utils::http_ok, MIME_JSON));
			}
		}
	private:
		py::dict reg;
};

/* Responder for files served out of the local filesystem.
 *
 * This code is NOT part of the "hot" path, so simplicity is more important
 * than performance.
 */
class DLL_LOCAL file_resource : public http_resource {
	public:
		file_resource(fs::path webroot) : webroot(webroot) {};

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
				if(verbose >= Verbose::UNEXPECTED)
					fmt::print(stderr, "Unable or unwilling to serve missing or non-file resource {}\n", path.string());

				return std::shared_ptr<http_response>(new string_response("No such file or directory.\n", http::http_utils::http_not_found));
			}

			/* Figure out a MIME type to use */
			std::string mime_type;
			try {
				mime_type = MIME_TYPES.at(path.extension().string());
			} catch(std::out_of_range &e) {
				if(verbose >= Verbose::UNEXPECTED)
					fmt::print(stderr, "Unable to determine MIME type for extension {}\n", path.extension().string());
				mime_type = "text/plain";
			}

			if(verbose >= Verbose::NOISY)
				fmt::print(stderr, "Serving {} with {} using MIME type {}\n", req.get_path(), path.string(), mime_type);

			/* Construct response and return it */
			auto response = std::shared_ptr<file_response>(new file_response(path.string(), http::http_utils::http_ok, mime_type));
			response->with_header(http::http_utils::http_header_cache_control, "max-age=3600"); /* Encourage caching */
			return response;
		}
	private:
		fs::path webroot;
};

/* Unfortunately, we need to carry around a global pointer just for signal handling. */
static webserver *ws_ref = NULL;
static void sigint(int signo) {
	if(ws_ref)
		ws_ref->stop();
}

int main(int argc, char **argv) {
	/*
	 * Parse command-line arguments
	 */

	int port;
	std::string preamble, registry, webroot;

	po::options_description desc("tuberd");
	desc.add_options()
		("help,h", "produce help message")

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

	/* Learn how the Python half lives */
	py::eval_file(preamble);

	/* Load indicated Python initialization scripts */
	py::eval_file(registry);

	/* Create a registry */
	py::dict reg = py::eval("registry");

	py::gil_scoped_release release;

	/*
	 * Start webserver
	 */

	webserver ws = create_webserver(port)
		.start_method(http::http_utils::THREAD_PER_CONNECTION)
		;
	ws_ref = &ws;
	std::signal(SIGINT, &sigint);

	/* Set up /tuber endpoint */
	tuber_resource tr(reg);
	tr.disallow_all();
	tr.set_allowing("POST", true);
	ws.register_resource("/tuber", &tr);

	/* If a valid webroot was provided, serve static content for other paths. */
        try {
	        file_resource fr(fs::canonical(webroot));
	        fr.disallow_all();
	        fr.set_allowing("GET", true);
	        ws.register_resource("/", &fr, true);
        } catch(fs::filesystem_error &e) {
                fmt::print(stderr, "Unable to resolve webroot {}; not serving static content.\n", webroot);
        }

	/* Go! */
	ws.start(true);
    
	return 0;
}
