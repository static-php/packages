# SPC Packages

Utility for building and packaging ZTS builds of PHP and shared extensions.
Published daily at https://pkg.henderkes.com. Uses [static-php-cli](https://github.com/crazywhalecc/static-php-cli)

## Usage for users

From a checkout, configure the repository for an explicit PHP version (8.2–8.6):

```sh
sudo sh bin/install-repositories 8.5
```

The script requires `curl`. It configures the PHP repository and the common library
repository on Debian/Ubuntu and Alpine; RPM uses the existing Static PHP repository.
Refresh the package index before installing or upgrading. The script does not install
PHP. PHP 8.6 must be selected explicitly because it is a prerelease stream.

Existing DEB/APK users must run the repository setup once before upgrading to shared
library packages. See [shared library packaging and migration](docs/shared-libraries.md)
for installation commands and repository administration.

## Usage for developers

The main command-line tool is `bin/spp` with the steps `build` and `package`. The former builds php and extensions, the latter packages them into os-packages.

### Build and Package

To run both build and package steps in one command:

```
php bin/spp all
```

To build and package the PHP-independent runtime libraries:

```sh
php bin/spp build --type=rpm --libs-only
php bin/spp package --type=rpm --libs-only
```

Use `--libraries=openssl,zlib` to select library packages; omit it for the complete
configured set. The same commands support `--type=deb` and `--type=apk`.

### Parameters

- `--target`: Optional. Specify the target architecture using a target triple that Zig understands, such as `native-native-gnu` or `native-native-musl -dynamic`. Default uses current OS defaults.
- `--type`: Required. Specify the package type to build. rpm, deb or apk.
- `--prefix`: Optional. Prefix for binaries and packages. `--prefix="-zts85"` generates `php-zts85` and `php-zts85-cli`. Defaults to `-zts`.
- `--packages`: Optional. Only build binaries and create packages for those packages. Default empty, builds everything.

## Links

- [static-php-cli](https://github.com/crazywhalecc/static-php-cli)
- [Static PHP Website](https://static-php.dev)
- [Henderkes PHP (ZTS) Packages](https://pkg.henderkes.com)

## Docker images for debugging CI failures

docker compose -f compose-debian.yaml run build "bin/spp all --phpv=8.4 --type=deb --debug"
