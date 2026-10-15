#include "tuber_support.hpp"

namespace py = pybind11;
using namespace pybind11::literals;

#include <algorithm>
#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <thread>

enum class Kind { X, Y };

/* State shared by concurrent calls into a Wrapper (and any copies of it, which
 * is why it lives behind a shared_ptr: a mutex member would make Wrapper
 * uncopyable). */
struct Counter {
	std::mutex mutex;
	long long value = 0;

	/* How many count() calls are running at once, and the most seen, so
	 * that tests can tell the lock was actually contended. */
	std::atomic<int> in_flight = 0;
	std::atomic<int> max_in_flight = 0;
};

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

		/* Add n to the shared counter, one step at a time. Each step is a
		 * read-modify-write with a yield in the middle, so concurrent
		 * callers would lose updates without the lock. */
		void count(int n) {
			int now = ++counter_->in_flight;
			int seen = counter_->max_in_flight;
			while (now > seen && !counter_->max_in_flight.compare_exchange_weak(seen, now))
				;

			/* Short calls rarely overlap by chance (with a GIL, the next
			 * request has to be dispatched in Python first), so wait
			 * (briefly) for a second caller: the lock is then contended
			 * whenever the server can run calls concurrently at all.
			 * Spin rather than sleep, so that both callers start
			 * counting together. */
			auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
			while (counter_->max_in_flight < 2 && std::chrono::steady_clock::now() < deadline)
				std::this_thread::yield();

			for (int i = 0; i < n; i++) {
				std::lock_guard<std::mutex> lock(counter_->mutex);
				long long v = counter_->value;
				std::this_thread::yield();
				counter_->value = v + 1;
			}

			--counter_->in_flight;
		}

		long long counter() const {
			std::lock_guard<std::mutex> lock(counter_->mutex);
			return counter_->value;
		}

		int max_in_flight() const { return counter_->max_in_flight; }

		void reset_counter() {
			std::lock_guard<std::mutex> lock(counter_->mutex);
			counter_->value = 0;
			counter_->max_in_flight = 0;
		}

	private:
		std::shared_ptr<Counter> counter_ = std::make_shared<Counter>();
};

#if PYBIND11_VERSION_HEX >= 0x020D0000
PYBIND11_MODULE(test_module, m, py::mod_gil_not_used()) {
#else
PYBIND11_MODULE(test_module, m) {
#endif

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
		/* The GIL is released, so calls overlap on every build and only
		 * the lock keeps the counter consistent. */
		.def("count", &Wrapper::count, "n"_a,
				"Add n to a shared counter, under a lock.",
				py::call_guard<py::gil_scoped_release>())
		.def("counter", &Wrapper::counter, "The shared counter's value.")
		.def("max_in_flight", &Wrapper::max_in_flight,
				"The most count() calls seen running at once.")
		.def("reset_counter", &Wrapper::reset_counter)
		;

	w.doc() = "This is the object DocString, defined in C++.";
}
