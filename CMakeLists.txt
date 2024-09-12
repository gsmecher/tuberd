# Build instructions for tuberd server only.  The client portion is installed
# via setuptools.

cmake_minimum_required(VERSION 3.18...3.22)
project(tuberd VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++17")

# build dependencies if requested
option(BUILD_DEPS "Build library dependencies")

if(BUILD_DEPS)
	message(STATUS "Building dependencies: ${CMAKE_SOURCE_DIR}/deps")
	execute_process(COMMAND ${CMAKE_SOURCE_DIR}/wheels/install_deps.sh
		RESULT_VARIABLE STATUS)
	if(STATUS AND NOT STATUS EQUAL 0)
		message(FATAL_ERROR "Failed to install dependencies: ${STATUS}")
	endif()
endif()

# link against deps dir
if(EXISTS ${CMAKE_SOURCE_DIR}/deps)
	message(STATUS "Found deps: ${CMAKE_SOURCE_DIR}/deps")
	list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/deps/share/cmake/Modules")
	list(APPEND CMAKE_PREFIX_PATH "${CMAKE_SOURCE_DIR}/deps")
endif()

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(LibHttpServer REQUIRED)
find_package(pybind11 REQUIRED)
find_package(Threads REQUIRED)

include(CTest)

pybind11_add_module(_tuber_runtime MODULE src/server.cpp)
target_include_directories(_tuber_runtime PUBLIC ${LIBHTTPSERVER_INCLUDE_DIRS})
target_link_libraries(_tuber_runtime PUBLIC ${LIBHTTPSERVER_LIBRARIES} Threads::Threads)

pybind11_add_module(test_module MODULE tests/test_module.cpp)
target_include_directories(test_module PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_test(NAME test-native-json
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py)

add_test(NAME test-orjson-fastpath
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py --orjson)

add_test(NAME test-doctests
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND python3 -m pytest tuber --doctest-modules)

set_tests_properties(test-native-json test-orjson-fastpath
	PROPERTIES ENVIRONMENT "CMAKE_TEST=1;PATH=${CMAKE_BINARY_DIR}:$ENV{PATH};PYTHONPATH=${CMAKE_BINARY_DIR}:${CMAKE_SOURCE_DIR}")

install(FILES "include/tuber_support.hpp" DESTINATION include)
