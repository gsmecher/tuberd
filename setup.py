# Setup instructions for client (Python module) only. The server portion is
# installed via CMake.

from setuptools import setup, find_packages

import os
import re
import subprocess
import sys
import sysconfig
import pybind11
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.install_scripts import install_scripts
from setuptools.command.install import install

# Convert distutils platform specifiers to CMake -A arguments
PLAT_TO_CMAKE = {
    "linux-x86_64": "x64",
}


# A CMakeExtension needs a sourcedir instead of a file list.
# The name must be the _single_ output extension from the CMake build.
# If you need multiple extensions, see scikit-build.
class CMakeExtension(Extension):
    def __init__(self, name: str, sourcedir: str = "") -> None:
        super().__init__(name, sources=[], optional=True)
        self.sourcedir = os.fspath(Path(sourcedir).resolve())


class CMakeBuild(build_ext):
    def build_extension(self, ext: CMakeExtension) -> None:
        if (sys.platform != "linux") and (not sys.platform.startswith("darwin")):
            raise DistutilsPlatformError("Cannot compile tuberd on non-Linux platform!")

        # Must be in this form due to bug in .resolve() only fixed in Python 3.10+
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()

        cmake_args = []
        build_args = []

        # Adding CMake arguments set as environment variable
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # sensible defaults
        if not any(["Python_ROOT_DIR" in a for a in cmake_args]):
            pyroot = sysconfig.get_config_var("prefix")
            cmake_args += [f"-DPython_ROOT_DIR={pyroot}"]

        if not any(["pybind11_DIR" in a for a in cmake_args]):
            pbdir = pybind11.get_cmake_dir()
            cmake_args += [f"-Dpybind11_DIR={pbdir}"]

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        subprocess.run(["cmake", ext.sourcedir, *cmake_args], cwd=build_temp, check=True)
        subprocess.run(["cmake", "--build", ".", *build_args], cwd=build_temp, check=True)
        self.tuberd_path = build_temp / "tuberd"

        # add test module
        build_lib = Path(self.build_lib) / "tuber" / "tests"
        if not build_lib.exists():
            build_lib.mkdir(parents=True)
        self.copy_file(build_temp / self.get_ext_filename("test_module"), build_lib)


class CMakeInstall(install_scripts):
    def run(self):
        super().run()

        self.announce("Installing tuberd", level=3)
        install_dir = Path(self.install_dir)
        if not install_dir.exists():
            install_dir.mkdir(parents=True)

        tuberd_src = self.get_finalized_command("build_ext").tuberd_path
        tuberd_dst = install_dir / "tuberd"
        self.copy_file(tuberd_src, tuberd_dst)


class CMakeInstallHeaders(install):
    def run(self):
        super().run()

        self.announce("Installing support headers", level=3)

        # Define the header files directory relative to the module
        src_root = os.path.dirname(os.path.realpath(__file__))
        headers_src = os.path.join(src_root, "include")
        headers_dst = os.path.join(self.install_lib, "tuber/include")

        # Create the destination directory if it does not exist
        os.makedirs(headers_dst, exist_ok=True)

        # Copy header files
        for header in os.listdir(headers_src):
            if header.endswith(".hpp"):
                self.copy_file(os.path.join(headers_src, header), os.path.join(headers_dst, header))


setup(
    ext_modules=[CMakeExtension("tuberd")],
    cmdclass={
        "build_ext": CMakeBuild,
        "install_scripts": CMakeInstall,
        "install": CMakeInstallHeaders,
    },
    packages=["tuber", "tuber.tests"],
    package_dir={
        "tuber": "./tuber",
        "tuber.tests": "./tests",
    },
    package_data={"tuber": ["include/*.hpp"]},
)
