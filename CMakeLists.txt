# Build instructions for tuberd server only.  The client portion is installed
# via setuptools.

cmake_minimum_required(VERSION 3.16...3.22)
project(tuberd VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} -std=c++17")

# link against deps dir
if(EXISTS ${CMAKE_SOURCE_DIR}/deps)
	message(STATUS "Found deps: ${CMAKE_SOURCE_DIR}/deps")
	list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/deps/share/cmake/Modules")
	list(APPEND CMAKE_PREFIX_PATH "${CMAKE_SOURCE_DIR}/deps")
endif()

find_package(Python COMPONENTS Interpreter Development REQUIRED)
find_package(fmt REQUIRED)
find_package(LibHttpServer REQUIRED)
find_package(pybind11 REQUIRED)
find_package(Threads REQUIRED)

include(CTest)

add_executable(tuberd src/server.cpp)
target_link_libraries(tuberd PRIVATE fmt::fmt ${LIBHTTPSERVER_LIBRARIES} pybind11::embed Threads::Threads)

pybind11_add_module(test_module MODULE tests/test_module.cpp)
target_include_directories(test_module PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(test_module PRIVATE fmt::fmt)

add_test(NAME test-native-json
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py)

add_test(NAME test-orjson-fastpath
	WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
	COMMAND tests/test.py --orjson-with-numpy)

set_tests_properties(test-native-json test-orjson-fastpath
	PROPERTIES ENVIRONMENT "PATH=${CMAKE_BINARY_DIR}:$ENV{PATH};PYTHONPATH=${CMAKE_BINARY_DIR}:${CMAKE_SOURCE_DIR}")

set_target_properties(tuberd PROPERTIES PUBLIC_HEADER "include/tuber_support.hpp")
install(TARGETS tuberd PUBLIC_HEADER DESTINATION include)
