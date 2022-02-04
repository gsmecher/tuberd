# Build instructions for tuberd server only.  The client portion is installed
# via setuptools.

cmake_minimum_required(VERSION 3.18...3.22)
project(tuber VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++20")

# Allow e.g. libhttpserver to be installed somewhere unexpected
list(APPEND CMAKE_MODULE_PATH "/usr/local/share/cmake/Modules")

find_package(Python COMPONENTS Interpreter Development)
find_package(Boost REQUIRED program_options)
find_package(fmt REQUIRED)
find_package(LibHttpServer MODULE REQUIRED)
find_package(pybind11 REQUIRED)
find_package(pybind11_json REQUIRED)

add_executable(tuberd src/server.cpp)
target_link_libraries(tuberd PRIVATE boost_program_options fmt::fmt httpserver pybind11::embed)

# The server only requires the preamble be installed - and outside of
# PYTHONPATH is fine.
install(FILES py/preamble.py DESTINATION /usr/share/tuberd)
install(TARGETS tuberd)
