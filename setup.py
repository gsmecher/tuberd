# Setup instructions for client (Python module) only. The server portion is
# installed via CMake.

from setuptools import setup

setup(
    name='tuber',
    version='0.10',
    author='Graeme Smecher',
    author_email='gsmecher@threespeedlogic.com',
    description='Serve Python (or C++) objects across a LAN using something like JSON-RPC',
    url='https://github.com/gsmecher/tuber',
    classifiers=[
        "Programming Language :: Python :: 3",
        "OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    packages=['tuber'],
    package_dir={'': 'py'},
)
