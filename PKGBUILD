# Maintainer: ds4tux contributors
# Contributor: James <james@home>
pkgname=ds4tux
pkgver=0.3.3
pkgrel=1
pkgdesc="DualShock 4 userspace driver for Linux — reliable, copycat-aware, Steam-friendly"
arch=('any')
url="https://github.com/.../ds4tux"
license=('MIT')
depends=(
    'python'
    'python-evdev'
    'python-pyudev'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-wheel'
)
source=()

prepare() {
    mkdir -p "$srcdir/$pkgname-$pkgver/src"
    cp -a "$startdir/pyproject.toml"       "$srcdir/$pkgname-$pkgver/"
    cp -a "$startdir/src/ds4tux"           "$srcdir/$pkgname-$pkgver/src/"
    cp -a "$startdir/udev"                 "$srcdir/$pkgname-$pkgver/"   # both rules
    cp -a "$startdir/systemd"              "$srcdir/$pkgname-$pkgver/"
    cp -a "$startdir/openrc"               "$srcdir/$pkgname-$pkgver/"
    cp -a "$startdir/ds4tux.conf.example"  "$srcdir/$pkgname-$pkgver/"
}

build() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$srcdir/$pkgname-$pkgver"
    python -m installer --destdir="$pkgdir" dist/*.whl

    # udev rules
    install -Dm644 udev/50-ds4tux.rules "$pkgdir/usr/lib/udev/rules.d/50-ds4tux.rules"
    install -Dm644 udev/50-ds4tux-audio.rules "$pkgdir/usr/lib/udev/rules.d/50-ds4tux-audio.rules"

    # systemd service
    install -Dm644 systemd/ds4tux.service "$pkgdir/usr/lib/systemd/system/ds4tux.service"

    # OpenRC service (installed regardless; unused on systemd distros)
    install -Dm755 openrc/ds4tux "$pkgdir/etc/init.d/ds4tux"

    # Default config
    install -Dm644 ds4tux.conf.example "$pkgdir/etc/ds4tux/config.toml"
}
