VERSION     := $(shell cat VERSION)
DEB_STAGING := build/pivideo_$(VERSION)_arm64
DEB_OUT     := build/pivideo_$(VERSION)_arm64.deb
DAEMON_BIN  := daemon/target/aarch64-unknown-linux-gnu/release/pivideo-daemon

.PHONY: build test build-cross deb image clean

# Local development build (host architecture, for running tests)
build:
	cd daemon && cargo build

test:
	cd daemon && cargo test

# Cross-compile daemon for Raspberry Pi (aarch64)
build-cross:
	cd daemon && cargo build --release --target aarch64-unknown-linux-gnu

# Package daemon + web server into a .deb for apt distribution
# Requires: dpkg-dev (Linux: apt-get install dpkg-dev; macOS: brew install dpkg)
deb: $(DAEMON_BIN)
	rm -rf $(DEB_STAGING)
	install -d $(DEB_STAGING)/DEBIAN
	install -d $(DEB_STAGING)/usr/local/bin
	install -d $(DEB_STAGING)/opt/pivideo/web
	sed 's/VERSION_PLACEHOLDER/$(VERSION)/' package/DEBIAN/control \
	    > $(DEB_STAGING)/DEBIAN/control
	install -m 755 package/DEBIAN/postinst $(DEB_STAGING)/DEBIAN/postinst
	install -m 755 package/DEBIAN/prerm    $(DEB_STAGING)/DEBIAN/prerm
	install -m 755 $(DAEMON_BIN)           $(DEB_STAGING)/usr/local/bin/pivideo-daemon
	install -m 644 web/server.py           $(DEB_STAGING)/opt/pivideo/web/server.py
	dpkg-deb --build --root-owner-group $(DEB_STAGING) $(DEB_OUT)
	@echo "==> Built: $(DEB_OUT)"

# Cross-compile, build .deb, and assemble the SD card image overlay
image: build-cross deb
	bash image/scripts/build.sh

clean:
	cd daemon && cargo clean
	rm -rf build/
