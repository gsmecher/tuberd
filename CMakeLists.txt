# Build instructions for tuberd server only.  The client portion is installed
# via setuptools.

cmake_minimum_required(VERSION 3.16...3.22)
project(tuber VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++20")

# Allow e.g. libhttpserver to be installed somewhere unexpected
list(APPEND CMAKE_MODULE_PATH "/usr/local/share/cmake/Modules")

find_package(Python COMPONENTS Interpreter Development)
find_package(fmt REQUIRED)
find_package(LibHttpServer MODULE REQUIRED)
find_package(pybind11 REQUIRED)

include(CTest)

add_executable(tuberd src/server.cpp)
target_link_libraries(tuberd PRIVATE fmt::fmt httpserver pybind11::embed)

pybind11_add_module(test_module MODULE tests/test_module.cpp)
target_include_directories(test_module PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_test(NAME test-native-json
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py)

add_test(NAME test-orjson-fastpath
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py --orjson-with-numpy)

set_tests_properties(test-native-json test-orjson-fastpath
	PROPERTIES ENVIRONMENT "CMAKE_BINARY_DIR=${CMAKE_BINARY_DIR};CMAKE_SOURCE_DIR=${CMAKE_SOURCE_DIR};PYTHONPATH=${CMAKE_BINARY_DIR}:${CMAKE_SOURCE_DIR}/py")

# The server only requires the preamble be installed - and outside of
# PYTHONPATH is fine.
set_target_properties(tuberd PROPERTIES PUBLIC_HEADER "include/tuber_support.hpp")

install(FILES py/preamble.py DESTINATION share/tuberd)
install(TARGETS tuberd PUBLIC_HEADER DESTINATION include)
