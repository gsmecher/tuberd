#!/bin/sh

# Figure out how to download things
if wget -q -O /dev/null http://www.google.com; then
    FETCH () {
        test -f `basename $1` || wget --no-check-certificate $1
    }
elif curl -Ls -o /dev/null http://www.google.com; then
    FETCH () {
        test -f `basename $1` || curl -kLO $1
    }
elif fetch -o /dev/null http://www.google.com; then
    FETCH () {
        test -f `basename $1` || fetch $1
    }
else
    echo "Cannot figure out how to download things!"
    exit 1
fi

set -e

scriptdir=$(cd `dirname $0`; pwd -P)
echo $scriptdir
cd $scriptdir/..

[ -d deps/lib ] || (mkdir -p deps/lib && cd deps && ln -s lib lib64)
prefix=$PWD/deps
cd $prefix

[ -e libmicrohttpd-1.0.1.tar.gz ] || FETCH https://github.com/Karlson2k/libmicrohttpd/releases/download/v1.0.1/libmicrohttpd-1.0.1.tar.gz
[ -e libmicrohttpd-1.0.1 ] || tar xzf libmicrohttpd-1.0.1.tar.gz
cd libmicrohttpd-1.0.1
./configure --with-pic --without-gnutls --enable-https=no --enable-shared=no --disable-doc --disable-examples --disable-tools --prefix=$prefix
make
make install
cd $prefix

[ -e libhttpserver ] || git clone https://github.com/etr/libhttpserver.git
cd libhttpserver
git checkout 0.19.0
[ -e configure ] || ./bootstrap
[ -e build ] || mkdir build
cd build
../configure --with-pic --enable-shared=no --disable-examples --prefix=$prefix CFLAGS=-I$prefix/include CXXFLAGS=-I$prefix/include LDFLAGS="-pthread -L$prefix/lib" || (
    cat config.log
    exit 1
)
make
make install
cd $prefix

cp $scriptdir/FindLibHttpServer.cmake $prefix/share/cmake/Modules/.
