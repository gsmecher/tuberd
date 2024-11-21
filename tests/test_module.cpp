#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "tuber_support.hpp"

namespace py = pybind11;
using namespace pybind11::literals;

#include <algorithm>

enum class Kind { X, Y };

class Wrapper {
	public:
		Kind return_x() const { return Kind::X; }
		Kind return_y() const { return Kind::Y; }

		bool is_x(Kind const& k) const { return k == Kind::X; }
		bool is_y(Kind const& k) const { return k == Kind::Y; }

		std::vector<int> increment(std::vector<int> x) {
			for (auto &i : x)
				i++;
			return x;
		};
};

PYBIND11_MODULE(test_module, m) {

	/* this forced scope ensures Kind is registered before it's used in
	 * default arguments below. */
	{
		py::str_enum<Kind> kind(m, "Kind");
		kind.value("X", Kind::X)
			.value("Y", Kind::Y);
	}

	auto w = py::class_<Wrapper>(m, "Wrapper")
		.def(py::init())
		.def("return_x", &Wrapper::return_x)
		.def("return_y", &Wrapper::return_y)
		.def("is_x", &Wrapper::is_x, "k"_a=Kind::X)
		.def("is_y", &Wrapper::is_y, "k"_a=Kind::Y)
		.def("increment", &Wrapper::increment,
				"x"_a,
				"A function that increments each element in its argument list.")
		.def("unserializable", [](const Wrapper &w) { return w; })
		;

	w.doc() = "This is the object DocString, defined in C++.";
}
