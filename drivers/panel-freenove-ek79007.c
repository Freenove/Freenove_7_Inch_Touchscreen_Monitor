// SPDX-License-Identifier: GPL-2.0
/*
 * panel-freenove-ek79007.c - DRM panel driver for the Freenove "7inch C"
 * MIPI-DSI display (EK79007 glass, 1024x600). Commented in detail on
 * purpose, since the file also serves as a walk-through of the DRM panel
 * and MIPI-DSI subsystems.
 *
 * The unusual part of this panel is that nothing powers it from the SoC.
 * Supply and reset lines are driven by a small MCU on the panel PCB (I2C
 * address 0x45), whose outputs are a 16-bit mask held in registers
 * 0x94/0x95:
 *
 *   bit 0 = AVDD (analog supply)  bit 1 = LCD_RESET
 *   bit 2 = BL_EN (backlight enable)  bit 4 = IOVCC (logic supply)
 *   bits 8/9 = VCC (main supply)
 *
 * The companion driver freenove-panel-regulator exposes those bits as a
 * gpio_chip, so this driver never sees a proprietary protocol: the overlay
 * binds reset-gpios / iovcc-gpios / avdd-gpios to it and the supplies are
 * handled with ordinary gpiod calls.
 *
 * Power-up order performed by prepare():
 *   1. IOVCC (logic supply) on, wait for it to settle
 *   2. AVDD (analog supply) on, wait for it to settle
 *   3. reset driven low for 60ms, then high, then 60ms of setup time
 *   4. vendor DCS register writes, sent while the link is in LP mode
 *   5. Sleep Out (0x11), wait 120ms
 *   6. Display On (0x29), wait 20ms
 *
 * vc4 needs all DCS traffic to be over before the host is enabled: the host
 * raises its HS clock as part of enable, and a command sent any later times
 * out with -110. prepare_prev_first below is what makes the bridge chain call
 * prepare() first, and without it the panel stays dark.
 */

#include <linux/delay.h>		/* msleep(), usleep_range() */
#include <linux/gpio/consumer.h>	/* gpiod_* consumer calls */
#include <linux/media-bus-format.h>	/* MEDIA_BUS_FMT_* */
#include <linux/module.h>		/* MODULE_* declarations */
#include <linux/of.h>			/* OF match tables */
#include <linux/property.h>		/* device property helpers */

#include <drm/drm_connector.h>		/* connector + display_info */
#include <drm/drm_mipi_dsi.h>		/* DSI device, DCS wrappers */
#include <drm/drm_modes.h>		/* drm_display_mode */
#include <drm/drm_of.h>			/* panel orientation from OF */
#include <drm/drm_panel.h>		/* drm_panel framework core */

/*
 * Driver private data.
 * base              - the drm_panel handed to the DRM core; the DRM callbacks
 *                     only receive that pointer, so container_of() below is
 *                     how we reach the rest of the state
 * dsi               - DSI device built by the mipi_dsi core from the
 *                     dsi_panel@0 child node of the overlay
 * reset/iovcc/avdd  - the three MCU mask bits this driver sequences; any of
 *                     them may be NULL when the overlay omits the property
 * orientation       - mounting orientation read from the DT rotation
 *                     property, forwarded to the connector in get_modes
 */
struct freenove_panel {
	struct drm_panel base;
	struct mipi_dsi_device *dsi;
	struct gpio_desc *reset;
	struct gpio_desc *iovcc;
	struct gpio_desc *avdd;
	enum drm_panel_orientation orientation;
};

/* Standard embedding idiom: get our struct back from the DRM one. */
static inline struct freenove_panel *to_freenove_panel(struct drm_panel *panel)
{
	return container_of(panel, struct freenove_panel, base);
}

/*
 * The one timing this glass has: 1024x600 at about 60 Hz.
 * Check with pixel clock = htotal * vtotal * refresh, i.e.
 * 1354 * 636 * 60 ~= 51.7 MHz.
 *
 * Blanking figures come from the panel datasheet: HFP/HSW/HBP = 160/10/160
 * and VFP/VSW/VBP = 12/1/23. DRM does not store blanking sizes, it stores
 * positions measured from the start of the active period, hence
 * hsync_start = hdisplay + HFP, hsync_end adds HSW, htotal adds HBP; the
 * vertical fields are the same idea counted in lines.
 */
static const struct drm_display_mode freenove_panel_mode = {
	.clock = 51700,		/* pixel clock in kHz */

	.hdisplay = 1024,
	.hsync_start = 1024 + 160,
	.hsync_end = 1024 + 160 + 10,
	.htotal = 1024 + 160 + 10 + 160,

	.vdisplay = 600,
	.vsync_start = 600 + 12,
	.vsync_end = 600 + 12 + 1,
	.vtotal = 600 + 12 + 1 + 23,
};

/*
 * EK79007 vendor register writes, as { address, value } pairs, which set up
 * the source/gate drivers, the internal supply voltages and the gamma curve.
 * These are opaque tuning registers taken from the vendor init code: they
 * only work as a complete set, so no value may be adjusted on its own.
 */
static const u8 freenove_panel_seq[][2] = {
	{ 0x80, 0x8b },
	{ 0x81, 0x78 },
	{ 0x82, 0x84 },
	{ 0x83, 0x88 },
	{ 0x84, 0xa8 },
	{ 0x85, 0xe3 },
	{ 0x86, 0x88 },
	{ 0x87, 0x5a },
	{ 0xb0, 0x80 },
	{ 0xb1, 0x30 },
	{ 0xb2, 0x50 },
};

/*
 * Send the panel init over DSI: the vendor register set, then the two
 * standard DCS commands that let the display come out of sleep and turn on.
 * The link is still in LP mode here, and every write is checked, because a
 * rejected command means the glass did not accept its configuration and the
 * remaining commands would only produce a garbled picture.
 */
static int freenove_panel_init(struct freenove_panel *ctx)
{
	struct mipi_dsi_device *dsi = ctx->dsi;
	unsigned int i;
	u8 value;
	int ret;

	/* one DCS generic write per table entry */
	for (i = 0; i < ARRAY_SIZE(freenove_panel_seq); i++) {
		value = freenove_panel_seq[i][1];
		ret = mipi_dsi_dcs_write(dsi, freenove_panel_seq[i][0],
					 &value, sizeof(value));
		if (ret < 0) {
			dev_err(&dsi->dev,
				"failed to write init reg 0x%02x: %d\n",
				freenove_panel_seq[i][0], ret);
			return ret;
		}
		dev_dbg(&dsi->dev, "init reg 0x%02x=0x%02x ok\n",
			freenove_panel_seq[i][0], value);
	}

	/* Sleep Out (0x11); the datasheet mandates 120ms before anything else */
	ret = mipi_dsi_dcs_exit_sleep_mode(dsi);	/* Opcode 0x11 */
	if (ret < 0) {
		dev_err(&dsi->dev, "sleep out (0x11) failed: %d\n", ret);
		return ret;
	}
	msleep(120);

	/* Display On (0x29); 20ms before the picture is actually driven */
	ret = mipi_dsi_dcs_set_display_on(dsi);		/* Opcode 0x29 */
	if (ret < 0) {
		dev_err(&dsi->dev, "display on (0x29) failed: %d\n", ret);
		return ret;
	}
	msleep(20);

	dev_info(&dsi->dev, "DCS init sequence completed\n");
	return 0;
}

/*
 * drm_panel prepare: put power on the glass and run its init. This is where
 * the whole bring-up starts, and it is ordered before the host by
 * prepare_prev_first (set in probe).
 */
static int freenove_panel_prepare(struct drm_panel *panel)
{
	struct freenove_panel *ctx = to_freenove_panel(panel);
	int ret;

	/* logic supply first, then analog supply, 20ms for each to settle */
	if (ctx->iovcc) {
		gpiod_set_value_cansleep(ctx->iovcc, 1);
		msleep(20);
	}

	if (ctx->avdd) {
		gpiod_set_value_cansleep(ctx->avdd, 1);
		msleep(20);
	}

	/* reset pulse: inactive 60ms, then active, then 60ms before DCS may start */
	if (ctx->reset) {
		gpiod_set_value_cansleep(ctx->reset, 0);
		msleep(60);
		gpiod_set_value_cansleep(ctx->reset, 1);
		msleep(60);
	}

	/* logging the rail levels is the quickest way to check the order in dmesg */
	dev_info(panel->dev,
		 "prepare: reset=%d iovcc=%d avdd=%d\n",
		 ctx->reset ? gpiod_get_value_cansleep(ctx->reset) : -1,
		 ctx->iovcc ? gpiod_get_value_cansleep(ctx->iovcc) : -1,
		 ctx->avdd ? gpiod_get_value_cansleep(ctx->avdd) : -1);

	/* the glass only accepts the init commands once its supplies are up */
	ret = freenove_panel_init(ctx);
	if (ret < 0) {
		dev_err(panel->dev, "failed to init panel: %d\n", ret);
		return ret;
	}

	dev_info(panel->dev, "prepare: panel powered up and initialized\n");
	return 0;
}

/*
 * drm_panel unprepare: the way back down. The panel is blanked and put to
 * sleep while it still has power, then the lines are released in the
 * reverse order of prepare().
 */
static int freenove_panel_unprepare(struct drm_panel *panel)
{
	struct freenove_panel *ctx = to_freenove_panel(panel);

	/* Display Off (0x28) then Sleep In (0x10), with 120ms for the internals */
	mipi_dsi_dcs_set_display_off(ctx->dsi);
	mipi_dsi_dcs_enter_sleep_mode(ctx->dsi);
	msleep(120);

	if (ctx->reset) {
		gpiod_set_value_cansleep(ctx->reset, 0);
		msleep(20);
	}

	if (ctx->avdd) {
		gpiod_set_value_cansleep(ctx->avdd, 0);
		msleep(20);
	}

	if (ctx->iovcc) {
		gpiod_set_value_cansleep(ctx->iovcc, 0);
		msleep(20);
	}

	return 0;
}

/*
 * drm_panel get_modes: publish what this panel can do. The DRM core calls
 * it while building the connector, and everything set here (mode, physical
 * size, bus format, orientation) decides how the display mode is created.
 */
static int freenove_panel_get_modes(struct drm_panel *panel,
				    struct drm_connector *connector)
{
	/* the only bus format the link carries */
	static const u32 bus_fmts[] = { MEDIA_BUS_FMT_RGB888_1X24 };
	struct drm_display_info *info = &connector->display_info;
	struct freenove_panel *ctx = to_freenove_panel(panel);
	struct drm_display_mode *mode;

	/* duplicate the const mode so the DRM core owns and frees the copy */
	mode = drm_mode_duplicate(connector->dev, &freenove_panel_mode);
	if (!mode)
		return 0;

	mode->type |= DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED;
	drm_mode_set_name(mode);		/* builds the standard "1024x600" mode name */
	drm_mode_probed_add(connector, mode);

	/* physical size in millimetres, used to derive DPI, not the picture */
	info->width_mm = 154;
	info->height_mm = 86;
	/* the DSI bridge needs format and depth to match dsi->format in probe */
	drm_display_info_set_bus_formats(info, bus_fmts,
					 ARRAY_SIZE(bus_fmts));
	connector->display_info.bpc = 8;

	/* pass the DT rotation on, so the compositor rotates the image for us */
	if (ctx->orientation != DRM_MODE_PANEL_ORIENTATION_UNKNOWN)
		drm_connector_set_panel_orientation(connector,
						    ctx->orientation);

	return 1;	/* the core expects the number of modes added */
}

/* Lets the DRM core ask how the panel is mounted, for its own rotation hints. */
static enum drm_panel_orientation freenove_panel_get_orientation(struct drm_panel *panel)
{
	struct freenove_panel *ctx = to_freenove_panel(panel);

	return ctx->orientation;
}

/* The drm_panel operations this driver provides. */
static const struct drm_panel_funcs freenove_panel_funcs = {
	.prepare = freenove_panel_prepare,
	.unprepare = freenove_panel_unprepare,
	.get_modes = freenove_panel_get_modes,
	.get_orientation = freenove_panel_get_orientation,
};

/*
 * mipi_dsi probe: the mipi_dsi core turns the overlay's dsi_panel@0 node
 * into a DSI device when the compatible matches, and then calls this. It is
 * pure set-up; the glass itself is not touched until the first prepare().
 */
static int freenove_panel_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct freenove_panel *ctx;
	int ret;

	/* devm_ ties the lifetime to the DSI device, so remove() frees nothing */
	ctx = devm_kzalloc(dev, sizeof(*ctx), GFP_KERNEL);
	if (!ctx)
		return -ENOMEM;

	mipi_dsi_set_drvdata(dsi, ctx);		/* picked up again by shutdown() and remove() */
	ctx->dsi = dsi;

	/* rotation is optional; UNKNOWN keeps get_modes from setting it */
	ctx->orientation = DRM_MODE_PANEL_ORIENTATION_UNKNOWN;
	of_drm_get_panel_orientation(dev->of_node, &ctx->orientation);

	/*
	 * The three lines come from the on-panel MCU, so each one may be
	 * absent and each lookup is optional. GPIOD_ASIS is deliberate: taking
	 * the GPIOs over would commit them to a level, while the whole point
	 * of this driver is to decide the order in prepare().
	 */
	ctx->reset = devm_gpiod_get_optional(dev, "reset", GPIOD_ASIS);
	if (IS_ERR(ctx->reset))
		return dev_err_probe(dev, PTR_ERR(ctx->reset),
				     "failed to get reset GPIO\n");

	ctx->iovcc = devm_gpiod_get_optional(dev, "iovcc", GPIOD_ASIS);
	if (IS_ERR(ctx->iovcc))
		return dev_err_probe(dev, PTR_ERR(ctx->iovcc),
				     "failed to get IOVCC GPIO\n");

	ctx->avdd = devm_gpiod_get_optional(dev, "avdd", GPIOD_ASIS);
	if (IS_ERR(ctx->avdd))
		return dev_err_probe(dev, PTR_ERR(ctx->avdd),
				     "failed to get AVDD GPIO\n");

	/* announce ourselves on a DSI-type connector */
	drm_panel_init(&ctx->base, dev, &freenove_panel_funcs,
		       DRM_MODE_CONNECTOR_DSI);

	/*
	 * The one line this driver cannot work without: it tells the core to
	 * run prepare() before enabling the host. vc4 has the HS clock up by
	 * the time the bridge starts, so init sent from enable() never gets
	 * through and the first DCS write returns -110.
	 */
	ctx->base.prepare_prev_first = true;

	/* the DT backlight phandle resolves to the MCU driver's backlight device */
	ret = drm_panel_of_backlight(&ctx->base);
	if (ret)
		return dev_err_probe(dev, ret, "failed to get backlight\n");

	drm_panel_add(&ctx->base);

	/*
	 * Link set-up, which has to agree with the panel datasheet:
	 *   VIDEO | VIDEO_HSE      - continuously driven video mode, sync
	 *                            pulses on every line
	 *   LPM                    - the init commands are sent while the link
	 *                            is still in low-power mode
	 *   CLOCK_NON_CONTINUOUS   - idle the HS clock, cheaper and less EMI
	 */
	dsi->mode_flags = MIPI_DSI_MODE_VIDEO_HSE | MIPI_DSI_MODE_VIDEO |
			  MIPI_DSI_MODE_LPM | MIPI_DSI_CLOCK_NON_CONTINUOUS;
	dsi->format = MIPI_DSI_FMT_RGB888;	/* must pair with the bus format reported in get_modes */
	dsi->lanes = 2;				/* the flex cable wires up two data lanes */

	/* bind to the host; if it refuses, the panel registration must not stay */
	ret = mipi_dsi_attach(dsi);
	if (ret < 0) {
		dev_err(dev, "failed to attach DSI device: %d\n", ret);
		drm_panel_remove(&ctx->base);
		return ret;
	}

	dev_info(dev, "panel probed, attached to DSI host, %u lanes\n",
		 dsi->lanes);
	return 0;
}

/* Runs on reboot/halt: drop reset and both supplies, so a half-torn-down display stack cannot leave the glass showing garbage. */
static void freenove_panel_shutdown(struct mipi_dsi_device *dsi)
{
	struct freenove_panel *ctx = mipi_dsi_get_drvdata(dsi);

	if (ctx->reset)
		gpiod_set_value_cansleep(ctx->reset, 0);
	if (ctx->avdd)
		gpiod_set_value_cansleep(ctx->avdd, 0);
	if (ctx->iovcc)
		gpiod_set_value_cansleep(ctx->iovcc, 0);
}

/* Unwinds probe from the far end: leave the bus first, then forget the panel. */
static void freenove_panel_remove(struct mipi_dsi_device *dsi)
{
	struct freenove_panel *ctx = mipi_dsi_get_drvdata(dsi);
	int ret;

	ret = mipi_dsi_detach(dsi);
	if (ret < 0)
		dev_err(&dsi->dev, "failed to detach DSI device: %d\n", ret);

	drm_panel_remove(&ctx->base);
}

/*
 * Matching rests on this string being byte-for-byte the compatible of the
 * dsi_panel@0 node in vc4-kms-dsi-freenove-panel.dts; a typo here means
 * probe is simply never called and the screen stays black.
 */
static const struct of_device_id freenove_panel_of_match[] = {
	{ .compatible = "freenove,ek79007-7inch-c" },
	{ }
};
MODULE_DEVICE_TABLE(of, freenove_panel_of_match);

/* DSI driver object; module_mipi_dsi_driver() writes the init/exit boilerplate. */
static struct mipi_dsi_driver freenove_panel_dsi_driver = {
	.probe = freenove_panel_probe,
	.remove = freenove_panel_remove,
	.shutdown = freenove_panel_shutdown,
	.driver = {
		.name = "panel-freenove-ek79007",
		.of_match_table = freenove_panel_of_match,
	},
};
module_mipi_dsi_driver(freenove_panel_dsi_driver);

MODULE_DESCRIPTION("DRM panel driver for the Freenove 7\" EK79007 DSI panel");
MODULE_AUTHOR("Ling Lin");
MODULE_LICENSE("GPL");
