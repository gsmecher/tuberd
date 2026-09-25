# Build instructions for tuberd server only.  The client portion is installed
# via setuptools.

cmake_minimum_required(VERSION 3.18...3.22)
project(tuberd VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++17")

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)

if(NOT pybind11_DIR)
  execute_process(
    COMMAND pybind11-config --cmakedir
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
  )
endif()
find_package(pybind11 REQUIRED)
find_package(Threads REQUIRED)

include(CTest)

pybind11_add_module(_tuber_runtime MODULE src/server.cpp)
target_include_directories(_tuber_runtime PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/extern/cpp-httplib)
target_link_libraries(_tuber_runtime PRIVATE Threads::Threads)

if(BUILD_TESTING)
	message(STATUS "Building tests")
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
		PROPERTIES ENVIRONMENT "CMAKE_TEST=1;PYTHONPATH=${CMAKE_BINARY_DIR}:${CMAKE_SOURCE_DIR}")
endif(BUILD_TESTING)

install(FILES "include/tuber_support.hpp" DESTINATION include)
