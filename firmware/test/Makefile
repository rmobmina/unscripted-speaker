# Host unit tests for the hardware-independent firmware core.
CORE := ../main/core
CC ?= cc
SAN ?= -fsanitize=undefined -fno-sanitize-recover=undefined
CFLAGS := -std=c11 -O1 -g -Wall -Wextra -Werror -Wno-unused-parameter $(SAN) -I$(CORE)
SRCS := test_core.c $(CORE)/sm.c $(CORE)/button_logic.c $(CORE)/led_engine.c $(CORE)/vad.c $(CORE)/byte_ring.c

.PHONY: test clean libled
test: build/test_core
	./build/test_core

build/test_core: $(SRCS) $(wildcard $(CORE)/*.h)
	@mkdir -p build
	$(CC) $(CFLAGS) -o $@ $(SRCS) -lm

# Shared library used by tools/led_simulator.py (identical renderer code).
libled: build/libled.dylib
build/libled.dylib: $(CORE)/led_engine.c $(CORE)/led_engine.h
	@mkdir -p build
	$(CC) -std=c11 -O2 -shared -fPIC -I$(CORE) -o $@ $(CORE)/led_engine.c -lm

clean:
	rm -rf build

# AddressSanitizer variant (may stall inside restricted sandboxes; fine in a normal terminal).
test-asan:
	$(MAKE) clean && $(MAKE) test SAN="-fsanitize=address,undefined"
