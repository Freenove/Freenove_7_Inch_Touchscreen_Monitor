#!/bin/bash
# Build and install the driver stack for the Freenove 7inch-C DSI panel in one
# go:
#
#   freenove-panel-regulator.ko      - the panel MCU on I2C (GPIOs + backlight)
#   panel-freenove-ek79007.ko        - DRM panel driver (power-up + DCS init)
#   vc4-kms-dsi-freenove-panel.dtbo  - device tree overlay
#
# For a Raspberry Pi 3/4/5 on Raspberry Pi OS Bookworm or Trixie:
#   sudo ./build.sh
# Re-running is safe: build/ is emptied and rebuilt from the sources each time,
# and the source directory never ends up holding build artifacts.
#
#   sudo ./build.sh --install-only
# Deployment without compiling: the .ko files and the .dtbo already sitting in
# build/ are copied into place (legacy module removed, depmod refreshed) and no
# apt package or kernel header is touched. install.py passes this when build/ is
# complete and not older than its sources, which is what lets a folder copied to
# another card still install its modules instead of skipping the whole run.
set -e   # stop at the first failing command, so a broken build cannot look like success

# Do every relative operation (source files, build/) from this script's own
# folder, so it works the same whether it is run from drivers/ or the project
# root, and install.py can call it without changing directory first.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OVERLAY_SRC="vc4-kms-dsi-freenove-panel.dts"    # the overlay source we compile
OVERLAY_DTBO="vc4-kms-dsi-freenove-panel.dtbo"  # the compiled form that goes into /boot/firmware/overlays/
EXTRA_DIR="/lib/modules/$(uname -r)/extra"      # where the running kernel expects add-on modules
MODULES="panel-freenove-ek79007.ko freenove-panel-regulator.ko"   # what the build produces

# Everything the build leaves behind (.o/.ko/.mod*/modules.order, plus the
# compiled .dtbo) lands in ./build/ and stays after installation, so a failure
# can be inspected without rebuilding.
BUILD_DIR="build"

# Under sudo, still compile as the user who ran the command: otherwise every
# artifact in build/ belongs to root and a later plain make stops at EACCES.
BUILD_AS="${SUDO_USER:-$(whoami)}"

INSTALL_ONLY=0
for arg in "$@"; do
	case "$arg" in
		--install-only) INSTALL_ONLY=1 ;;
		*) echo "Error: unknown option '$arg'; the only option is --install-only" >&2; exit 2 ;;
	esac
done

# When nothing will be compiled, every artifact has to be there already. Say so
# before touching the system rather than halfway through the deployment.
if [ "$INSTALL_ONLY" = 1 ]; then
	for artifact in ${MODULES} "${OVERLAY_DTBO}"; do
		if [ ! -f "${BUILD_DIR}/${artifact}" ]; then
			echo "Error: ${BUILD_DIR}/${artifact} is missing; run ./build.sh without --install-only first" >&2
			exit 1
		fi
	done
fi

echo "== [1/5] Installing build dependencies =="
if [ "$INSTALL_ONLY" = 1 ]; then
	# dtc and the headers exist to serve the compiles, so this half is a no-op.
	echo "-- install-only: nothing is compiled, no package installed"
else
	sudo apt update
	sudo apt install -y device-tree-compiler   # provides dtc, needed for the overlay

	# Kernel headers are named differently per distribution: Trixie has
	# linux-headers-rpi-{v6,v7,v7l,v8} with the CPU architecture as the suffix,
	# Bookworm and earlier have raspberrypi-kernel-headers. The build symlink for
	# the running kernel is the proof that headers are already in place, so that
	# case is skipped entirely.
	if [ ! -e "/lib/modules/$(uname -r)/build" ]; then
		HDR_SUFFIX="$(uname -r | sed 's/.*rpi-//')"   # the architecture suffix at the end of the kernel release, e.g. v8 or v7l
		if ! sudo apt install -y "linux-headers-rpi-${HDR_SUFFIX}"; then
			sudo apt install -y raspberrypi-kernel-headers   # the name used before Trixie
		fi
	fi
	if [ ! -e "/lib/modules/$(uname -r)/build" ]; then
		echo "Error: no kernel headers found for $(uname -r); cannot build the modules"
		exit 1
	fi
fi

echo "== [2/5] Removing first-generation legacy module =="
# The other driver written for this panel registers the same I2C 0x45 node, so
# the two cannot coexist: the MCU would end up bound to whichever loses the
# race. This has to happen either way, which is why the mode does not skip it.
if [ -e "${EXTRA_DIR}/panel-freenove-dsi.ko" ]; then
	sudo rm "${EXTRA_DIR}/panel-freenove-dsi.ko"
	echo "removed panel-freenove-dsi.ko"
fi

echo "== [3/5] Building and installing kernel modules =="
if [ "$INSTALL_ONLY" = 1 ]; then
	# The deploy recipe lives in the Makefile, and build/ is where make is pointed,
	# so the Makefile goes along with it.
	cp Makefile "${BUILD_DIR}/"
	sudo make -C "${BUILD_DIR}" install-only
else
	# Kbuild writes its artifacts into the directory named by M=, so the sources are
	# copied into build/ and compiled there: the source tree stays clean and one
	# rm -rf accounts for the whole build.
	rm -rf "${BUILD_DIR}"
	runuser -u "${BUILD_AS}" -- mkdir -p "${BUILD_DIR}"
	runuser -u "${BUILD_AS}" -- cp Makefile panel-freenove-ek79007.c \
		freenove-panel-regulator.c "${BUILD_DIR}/"
	runuser -u "${BUILD_AS}" -- make -C "${BUILD_DIR}"       # compile both modules
	sudo make -C "${BUILD_DIR}" install                      # copy into extra/ and regenerate the dependency list
fi

echo "== [4/5] Building and deploying the device tree overlay =="
if [ "$INSTALL_ONLY" = 0 ]; then
	# -@ emits the symbol table an overlay needs; the .dtbo goes into build/ first
	runuser -u "${BUILD_AS}" -- dtc -@ -I dts -O dtb \
		-o "${BUILD_DIR}/${OVERLAY_DTBO}" "${OVERLAY_SRC}"
fi
sudo cp "${BUILD_DIR}/${OVERLAY_DTBO}" /boot/firmware/overlays/

echo "== [5/5] Keeping build artifacts =="
# Left untouched on purpose: the objects, module metadata and the .dtbo stay in
# ./${BUILD_DIR}/, which is what makes a panel that stays dark after a reboot
# still inspectable.

echo ""
if [ "$INSTALL_ONLY" = 1 ]; then
	echo "Done, deployed from ${BUILD_DIR}/ without compiling."
else
	echo "Done. Modules installed to: ${EXTRA_DIR}/"
fi
echo "Build artifacts kept in: $(pwd)/${BUILD_DIR}/"
echo "Please make sure /boot/firmware/config.txt contains:"
echo "    dtoverlay=vc4-kms-v3d"
echo "    dtoverlay=vc4-kms-dsi-freenove-panel"
echo "(DSI0 is used by default; for the DSI1 connector write 'dtoverlay=vc4-kms-dsi-freenove-panel,dsi1')"
echo "Then reboot the system."
