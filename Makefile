.PHONY: build test image clean

build:
	cd daemon && cargo build

test:
	cd daemon && cargo test

# Cross-compile for Pi and prepare image overlay
image:
	bash image/scripts/build.sh

clean:
	cd daemon && cargo clean
