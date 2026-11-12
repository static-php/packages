# SPC Packages

Utility for building and packaging ZTS builds of PHP and shared extensions.
Published daily at https://pkg.henderkes.com. Uses [static-php-cli](https://github.com/crazywhalecc/static-php-cli)

## Usage for users

From a checkout, configure the repository for an explicit PHP version (8.2–8.6):

```sh
sudo sh bin/install-repositories 8.5
```

The script requires `curl`. It configures the selected PHP repository, which also
contains its shared-library dependencies; RPM uses the existing Static PHP repository.
Refresh the package index before installing or upgrading. The script does not install
PHP. PHP 8.6 must be selected explicitly because it is a prerelease stream.

Existing users keep their repository configuration and migrate through ordinary
`apt upgrade`, `dnf upgrade` or `apk upgrade`.

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
configured set. Compile glibc payloads on EL8 only, then package the same sealed binaries as RPM
and DEB. Alpine uses its separate native-musl build. The shared glibc workflow
produces both package formats without a second compilation.

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

Debian images provide packaging and runtime-test tools; production glibc
compilation runs in the EL8 builder. Use the shared glibc workflow with
`publish=false` to build and validate without updating repositories.

Publish an existing validated build with `publish-packages.yml`, selecting one
format at a time. RPM and DEB use the same shared EL8 build run ID; APK uses its
Alpine build run ID:

```sh
gh workflow run publish-packages.yml --ref master -f format=rpm -f build_run_id="$GLIBC_RUN_ID"
gh workflow run publish-packages.yml --ref master -f format=deb -f build_run_id="$GLIBC_RUN_ID"
gh workflow run publish-packages.yml --ref master -f format=apk -f build_run_id="$ALPINE_RUN_ID"
```

This publishes the built versions without compilation or repackaging. Build runs
must be completed on the default branch and still have their package artifacts,
retained for seven days. Existing artifacts keep their original expiry. Publication
rechecks upgrade receipts and current repository versions before uploading.
