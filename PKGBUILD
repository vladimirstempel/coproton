# Maintainer: Vladimir Stempel <16229503+vladimirstempel@users.noreply.github.com>
pkgname=coproton
pkgver=0.1.0
pkgrel=1
pkgdesc="Run a Steam game together with a trainer in the same Proton prefix"
arch=('any')
url="https://github.com/vladimirstempel/coproton"
license=('MIT')
depends=('python')
optdepends=('winetricks: install .NET into the game prefix')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

check() {
  cd "$pkgname-$pkgver"
  python3 launcher.py --selftest
}

package() {
  cd "$pkgname-$pkgver"
  install -Dm755 launcher.py          "$pkgdir/usr/lib/$pkgname/launcher.py"
  install -Dm644 toolmanifest.vdf     "$pkgdir/usr/lib/$pkgname/toolmanifest.vdf"
  install -Dm644 compatibilitytool.vdf "$pkgdir/usr/lib/$pkgname/compatibilitytool.vdf"
  install -Dm644 README.md            "$pkgdir/usr/share/doc/$pkgname/README.md"
  install -d "$pkgdir/usr/bin"
  ln -s "/usr/lib/$pkgname/launcher.py" "$pkgdir/usr/bin/$pkgname"
}
