# Setup instructions for client (Python module) only. The server portion is
# installed via CMake.

from setuptools import setup, find_packages

import os
import re
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.install_scripts import install_scripts

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

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        subprocess.run(["cmake", ext.sourcedir, *cmake_args], cwd=build_temp, check=True)
        subprocess.run(["cmake", "--build", ".", *build_args], cwd=build_temp, check=True)
        self.tuberd_path = build_temp / "tuberd"


class CMakeInstall(install_scripts):
    def run(self):
        self.announce("Installing tuberd", level=3)

        install_dir = Path(self.install_dir)
        if not install_dir.exists():
            install_dir.mkdir(parents=True)

        tuberd_src = self.get_finalized_command("build_ext").tuberd_path
        tuberd_dst = install_dir / "tuberd"
        self.copy_file(tuberd_src, tuberd_dst)
        super().run()


setup(
    ext_modules=[CMakeExtension("tuberd")],
    cmdclass={
        "build_ext": CMakeBuild,
        "install_scripts": CMakeInstall,
    },
    packages=find_packages(),
)
