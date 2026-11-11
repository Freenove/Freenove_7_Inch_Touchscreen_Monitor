# Build file for the two out-of-tree modules of the Freenove "7inch C" DSI
# panel (EK79007 glass):
#
#   panel-freenove-ek79007.ko    - DRM panel driver, power-up order + DCS init
#   freenove-panel-regulator.ko  - the panel MCU on I2C, GPIOs + backlight
#
# On the Pi, kernel headers matching the running kernel are needed first:
#     sudo apt install raspberrypi-kernel-headers     # Bookworm and earlier
#     sudo apt install linux-headers-rpi-v8           # Trixie: v6/v7/v7l/v8 per CPU
#     make              # both .ko files
#     sudo make install # drop them into extra/ and run depmod
#     sudo make install-only   # the same copy step, but nothing is compiled
#

# obj-m lists what Kbuild should build as loadable modules; each name is
# resolved to a source file of the same name (panel-freenove-ek79007.o from
# panel-freenove-ek79007.c).
obj-m := panel-freenove-ek79007.o freenove-panel-regulator.o

# The kernel tree to build against. ?= keeps a value coming from the
# environment or the command line, so an unusual header path needs no edit here.
KDIR ?= /lib/modules/$(shell uname -r)/build
# Handed to Kbuild as M=: it is both where the module sources are read from
# and where the .o/.ko/.mod artifacts end up.
PWD  := $(shell pwd)

# What a bare "make" does
all: modules

# -C switches into the kernel tree, M= points it back here
modules:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

# Install: build first, then hand the result to install-only. Keeping the two
# apart is what lets build.sh re-deploy a build/ that is already current
# without compiling it again.
install: modules install-only

# Copy the .ko files that are already in this directory into extra/ and refresh
# the dependency index with depmod. Afterwards udev loads them by itself from the
# device tree compatible strings, so nothing has to be modprobe'd by hand. No
# kernel headers are needed here, which is exactly why it works without a build.
install-only:
	# Trixie's linux-headers-rpi-* packages ship no extra/ directory, and
	# install then reads the trailing extra/ as a file name and refuses with
	# "Not a directory". Creating the path up front avoids that, and
	# install -d behaves like mkdir -p, so it does not complain when
	# the directory is already there.
	sudo install -d /lib/modules/$(shell uname -r)/extra
	sudo install -m 0644 panel-freenove-ek79007.ko /lib/modules/$(shell uname -r)/extra/
	sudo install -m 0644 freenove-panel-regulator.ko /lib/modules/$(shell uname -r)/extra/
	sudo depmod -a

# Clean: have Kbuild remove exactly what it produced in this directory
clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

# These are actions rather than files, and must not be shadowed by a file of
# the same name
.PHONY: all modules install install-only clean
