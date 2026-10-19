// SPDX-License-Identifier: GPL-2.0
/*
 * freenove-panel-regulator.c - driver for the MCU on the Freenove
 * "7inch C" DSI panel board (I2C address 0x45). Commented in detail because
 * the protocol behind it is proprietary and documented nowhere public.
 *
 * The MCU looks nothing like a regulator. Its outputs form one 16-bit
 * pattern held in two registers, 0x94 for bits 15..8 and 0x95 for bits 7..0,
 * and the pattern can never be read back:
 *
 *   bit 0   AVDD, the glass analog supply
 *   bit 1   LCD_RESET
 *   bit 2   BL_EN, the backlight enable gate
 *   bit 4   IOVCC, the glass logic supply
 *   bits 8/9 VCC, the board supply; both bits have to be set together
 *   reg 0x96 - backlight PWM duty, 0..255
 *
 * Instead of inventing a private interface for the panel driver, this driver
 * reshapes that protocol into two frameworks the kernel already has:
 *   - a 16-line gpio_chip, so the overlay can write <&panel_mcu 1 0> for
 *     LCD_RESET and the panel driver works with plain gpiod descriptors;
 *   - a backlight device around register 0x96, so brightness stays a write
 *     to /sys/class/backlight/<dev>/brightness;
 *   - the optional enable-gpios property, which points back at our own bit 2
 *     and gates the supply in front of the PWM.
 *
 * probe() raises VCC and IOVCC. IOVCC comes up here because the same rail
 * feeds the GT911 touch, which has to be powered before the goodix driver
 * reads its config; the AVDD supply and the glass reset still belong to the
 * panel driver's prepare(), so the panel power-up order lives in exactly one
 * place. The DT pre-power-on property additionally fronts AVDD and reset for
 * setups that want an image as early as possible.
 */

#include <linux/backlight.h>		/* backlight_device_register() */
#include <linux/err.h>			/* error pointer tests */
#include <linux/fb.h>			/* FB_BLANK_*, BL_CORE_* */
#include <linux/gpio.h>			/* GPIO_LINE_DIRECTION_IN/OUT */
#include <linux/gpio/consumer.h>/* gpiod_set_value_cansleep() */
#include <linux/gpio/driver.h>	/* struct gpio_chip */
#include <linux/i2c.h>			/* i2c_transfer(), i2c_client */
#include <linux/module.h>		/* MODULE_* declarations */
#include <linux/of.h>			/* OF match tables */
#include <linux/property.h>		/* device property access */
#include <linux/regmap.h>		/* regmap_init_i2c(), regmap_write() */
#include <linux/regulator/driver.h>	/* devm_regulator_register(), struct regulator_desc */
#include <linux/version.h>		/* LINUX_VERSION_CODE, KERNEL_VERSION() */

/* The register map of the panel MCU. */
#define FPN_REG_TP		0x94	/* mask bits 15..8 */
#define FPN_REG_LCD		0x95	/* mask bits 7..0 */
#define FPN_REG_PWM		0x96	/* backlight duty, 0..255 */
#define FPN_REG_SIZE    0x97	/* read-only panel size code */
#define FPN_REG_ID		0x98	/* read-only hardware identity */
#define FPN_REG_VERSION	0x99	/* read-only firmware revision */

#define FPN_NUM_GPIO		16	/* one emulated GPIO line per mask bit */

/* What each mask bit drives, taken from the panel schematic. */
#define FPN_BIT_AVDD		BIT(0)		/* analog supply for the glass */
#define FPN_BIT_LCD_RESET	BIT(1)		/* glass reset, asserted high */
#define FPN_BIT_BL_EN		BIT(2)		/* backlight power gate */
#define FPN_BIT_IOVCC		BIT(4)		/* logic supply for the glass */
#define FPN_BIT_VCC		(BIT(8) | BIT(9))	/* board supply, usable only with both bits set */

/*
 * Driver private state.
 * dir_lock        - guards direction_state
 * pwr_lock        - guards poweron_state and keeps one mask update to a
 *                   single critical section, because changing a bit means
 *                   reading the mirror, editing it and writing all 16 bits
 *                   back to the MCU
 * regmap          - register access handle
 * poweron_state   - the only copy of the mask that exists: the MCU cannot be
 *                   read back, so this shadow is the driver's truth
 * direction_state - input/output bookkeeping, something the hardware has no
 *                   notion of
 * gc              - the gpio_chip handed to the GPIO core
 * enable          - descriptor for the optional BL_EN gate, our own bit 2
 */
struct freenove_panel_mcu {
	struct mutex dir_lock;
	struct mutex pwr_lock;
	struct regmap *regmap;
	u16 poweron_state;
	u16 direction_state;

	struct gpio_chip gc;
	struct gpio_desc *enable;
	struct regulator_dev *iovcc_rdev;
};

/* Byte address, byte value, nothing past 0x96 reachable through regmap; the identity registers above it are read with i2c_transfer() instead. */
static const struct regmap_config freenove_panel_regmap_config = {
	.reg_bits = 8,
	.val_bits = 8,
	.max_register = FPN_REG_PWM,
};

/*
 * Push the mask as two byte writes, 0x94 before 0x95. The mask is only
 * meaningful as a whole, so both registers are always written together from
 * the same value.
 */
static void freenove_panel_write_state(struct freenove_panel_mcu *state,
				       u16 value)
{
	regmap_write(state->regmap, FPN_REG_TP, value >> 8);
	regmap_write(state->regmap, FPN_REG_LCD, value & 0xff);
}

/*
 * --- gpio_chip implementation ---
 * A line number is a mask bit number, so offset N is bit N and the overlay
 * reference <&panel_mcu 1 0> reads as "bit 1, LCD_RESET, active high".
 * Everything below works on the shadow mask, never on a hardware read.
 */

/* Marking a line input is bookkeeping only: the bit drives regardless, and get() still answers from the mirror. */
static int freenove_panel_gpio_direction_in(struct gpio_chip *gc,
					    unsigned int offset)
{
	struct freenove_panel_mcu *state = gpiochip_get_data(gc);

	mutex_lock(&state->dir_lock);
	state->direction_state |= BIT(offset);
	mutex_unlock(&state->dir_lock);

	return 0;
}

/* Output with an initial level: update the mirror first, then send the complete mask to the MCU. */
static int freenove_panel_gpio_direction_out(struct gpio_chip *gc,
					     unsigned int offset, int val)
{
	struct freenove_panel_mcu *state = gpiochip_get_data(gc);
	u16 value;

	mutex_lock(&state->dir_lock);
	state->direction_state &= ~BIT(offset);
	mutex_unlock(&state->dir_lock);

	/* mirror, edit and write-back belong to one critical section */
	mutex_lock(&state->pwr_lock);
	value = state->poweron_state;
	if (val)
		value |= BIT(offset);
	else
		value &= ~BIT(offset);
	state->poweron_state = value;
	freenove_panel_write_state(state, value);
	mutex_unlock(&state->pwr_lock);

	return 0;
}

/* Direction comes from the cache; the MCU has no direction at all. */
static int freenove_panel_gpio_get_direction(struct gpio_chip *gc,
					     unsigned int offset)
{
	struct freenove_panel_mcu *state = gpiochip_get_data(gc);
	u16 dirs;

	mutex_lock(&state->dir_lock);
	dirs = state->direction_state;
	mutex_unlock(&state->dir_lock);

	return (dirs & BIT(offset)) ? GPIO_LINE_DIRECTION_IN
				    : GPIO_LINE_DIRECTION_OUT;
}

/* Nothing can be read back from the MCU, so the mirrored bit is the answer. */
static int freenove_panel_gpio_get(struct gpio_chip *gc, unsigned int offset)
{
	struct freenove_panel_mcu *state = gpiochip_get_data(gc);
	int value;

	mutex_lock(&state->pwr_lock);
	value = !!(state->poweron_state & BIT(offset));
	mutex_unlock(&state->pwr_lock);

	return value;
}

/*
 * struct gpio_chip ->set() has changed return type twice: int up to 6.8, void
 * across 6.9..6.16 ("gpiolib: get rid of the return value of ->set()"), and
 * int again from 6.17 (the revert lets an I2C GPIO expander report a failed
 * transfer). Raspberry Pi OS only ever ships one side of each boundary -- 6.6,
 * then 6.12/6.16, then 6.17/6.18 -- so this window picks the matching
 * signature on every image the panel targets, with no cast or guesswork.
 */
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 9, 0) && \
    LINUX_VERSION_CODE <  KERNEL_VERSION(6, 17, 0)
#define FREENOVE_GPIO_SET_VOID
#endif

/*
 * Set a line level, the entry point the panel driver uses for its supplies
 * and for reset. Under pwr_lock: take the mirror, flip the one bit, store it
 * back, then push the whole mask over I2C. The GPIO core bounds-checks offset
 * against ngpio before calling, so a line past the last is simply ignored.
 */
#ifdef FREENOVE_GPIO_SET_VOID
static void freenove_panel_gpio_set(struct gpio_chip *gc, unsigned int offset,
				    int value)
#else
static int freenove_panel_gpio_set(struct gpio_chip *gc, unsigned int offset,
				   int value)
#endif
{
	struct freenove_panel_mcu *state = gpiochip_get_data(gc);
	u16 last_val;

	if (offset >= FPN_NUM_GPIO)
#ifdef FREENOVE_GPIO_SET_VOID
		return;
#else
		return -EINVAL;
#endif

	mutex_lock(&state->pwr_lock);
	last_val = state->poweron_state;
	if (value)
		last_val |= BIT(offset);
	else
		last_val &= ~BIT(offset);
	state->poweron_state = last_val;
	freenove_panel_write_state(state, last_val);
	mutex_unlock(&state->pwr_lock);

#ifndef FREENOVE_GPIO_SET_VOID
	return 0;
#endif
}

/*
 * --- backlight implementation ---
 * Brightness is written straight into 0x96 as a duty value. Where the
 * overlay supplied an enable gate (bit 2), it follows the same callback, so
 * brightness 0 takes the backlight supply off instead of only asking the PWM
 * for a zero duty.
 */
static int freenove_panel_update_status(struct backlight_device *bl)
{
	struct freenove_panel_mcu *state = bl_get_data(bl);
	int brightness = bl->props.brightness;

	/* the core wants the display dark, whatever brightness says */
	if (bl->props.power != FB_BLANK_UNBLANK ||
	    bl->props.state & (BL_CORE_SUSPENDED | BL_CORE_FBBLANK))
		brightness = 0;

	/* the gate follows brightness, not the raw duty value */
	if (state->enable)
		gpiod_set_value_cansleep(state->enable, !!brightness);

	/* 0..255 goes into the register unchanged */
	return regmap_write(state->regmap, FPN_REG_PWM, brightness);
}

static const struct backlight_ops freenove_panel_bl_ops = {
	.update_status = freenove_panel_update_status,
};

/*
 * Reading the MCU is not a normal I2C read: the register has to be written
 * as a transaction of its own, then a short pause, then a second transaction
 * collects the value. Repeated-start compound transfers are not accepted,
 * which is why regmap_read() cannot be used and the sequence is done by hand
 * here.
 */
static int freenove_panel_i2c_read(struct i2c_client *client, u8 reg,
				   unsigned int *buf)
{
	struct i2c_msg msg = {
		.addr = client->addr,
		.len = 1,
	};
	u8 addr_buf = reg;
	u8 data_buf = 0;
	int ret;

	/* phase 1: select the register */
	msg.buf = &addr_buf;
	ret = i2c_transfer(client->adapter, &msg, 1);
	if (ret != 1)
		return -EIO;

	/* the MCU needs a few ms to place the value on its output */
	usleep_range(5000, 10000);

	/* phase 2: a separate read transaction collects it */
	msg.flags = I2C_M_RD;
	msg.buf = &data_buf;
	ret = i2c_transfer(client->adapter, &msg, 1);
	if (ret != 1)
		return -EIO;

	*buf = data_buf;
	return 0;
}

/*
 * --- IOVCC offered as a regulator ---
 * GT911 touch is fed from the same IOVCC rail (bit 4) the glass uses, and
 * that rail is normally only raised by the panel driver's prepare(). On a
 * cold boot the in-tree goodix driver can probe for the touch before IOVCC
 * is up, so its config read returns the chip's power-on defaults (a 4096
 * range instead of this panel's 800x480) and touch works but maps wrong; a
 * warm reboot hides this because the board never lost power, so IOVCC was
 * already on. Exposing bit 4 as a regulator and referencing it from the
 * touch node makes the kernel defer the goodix probe (EPROBE_DEFER) until
 * this driver has probed and the supply is real, removing the race.
 *
 * One bit, two consumers: the panel drives bit 4 through its iovcc-gpios
 * descriptor and the touch enables it through this regulator. Both only ever
 * ask for the bit high while the display is up, and the shadow-mask write is
 * serialised by pwr_lock, so the two paths stay consistent.
 */
static void freenove_panel_set_bit(struct freenove_panel_mcu *state,
				   unsigned int bit, bool on)
{
	u16 value;

	mutex_lock(&state->pwr_lock);
	value = state->poweron_state;
	if (on)
		value |= BIT(bit);
	else
		value &= ~BIT(bit);
	state->poweron_state = value;
	freenove_panel_write_state(state, value);
	mutex_unlock(&state->pwr_lock);
}

static int freenove_panel_iovcc_enable(struct regulator_dev *rdev)
{
	freenove_panel_set_bit(rdev_get_drvdata(rdev), 4, true);
	return 0;
}

static int freenove_panel_iovcc_disable(struct regulator_dev *rdev)
{
	freenove_panel_set_bit(rdev_get_drvdata(rdev), 4, false);
	return 0;
}

static int freenove_panel_iovcc_is_enabled(struct regulator_dev *rdev)
{
	struct freenove_panel_mcu *state = rdev_get_drvdata(rdev);
	bool on;

	mutex_lock(&state->pwr_lock);
	on = state->poweron_state & FPN_BIT_IOVCC;
	mutex_unlock(&state->pwr_lock);

	return on;
}

static const struct regulator_ops freenove_panel_iovcc_ops = {
	.enable = freenove_panel_iovcc_enable,
	.disable = freenove_panel_iovcc_disable,
	.is_enabled = freenove_panel_iovcc_is_enabled,
};

static const struct regulator_desc freenove_panel_iovcc_desc = {
	.name = "vddio",
	.id = 0,
	.ops = &freenove_panel_iovcc_ops,
	.type = REGULATOR_VOLTAGE,
	.owner = THIS_MODULE,
};

/*
 * probe: runs once the panel-mcu@45 node created by the overlay matches our
 * compatible string and the I2C core has made a client out of it.
 *
 * One ordering rule inside this function: the gpio_chip has to be registered
 * before we look up our own enable-gpios, because that descriptor points
 * into this very chip. Done the other way round there is no provider to ask,
 * so the lookup cannot even answer with -EPROBE_DEFER.
 */
static int freenove_panel_i2c_probe(struct i2c_client *i2c)
{
	struct backlight_properties props = {
		.type = BACKLIGHT_RAW,	/* the MCU duty value is the whole brightness range */
		.max_brightness = 255,	/* 8-bit duty register, so 0..255 */
	};
	struct backlight_device *bl;
	struct freenove_panel_mcu *state;
	unsigned int data;
	bool pre_power_on;
	int ret;

	state = devm_kzalloc(&i2c->dev, sizeof(*state), GFP_KERNEL);
	if (!state)
		return -ENOMEM;

	mutex_init(&state->dir_lock);
	mutex_init(&state->pwr_lock);
	i2c_set_clientdata(i2c, state);

	/* every mask and duty write from here on goes through regmap */
	state->regmap = devm_regmap_init_i2c(i2c,
					     &freenove_panel_regmap_config);
	if (IS_ERR(state->regmap))
		return dev_err_probe(&i2c->dev, PTR_ERR(state->regmap),
				     "failed to allocate register map\n");

	/* pure information: these reads may fail, and nothing depends on them */
	if (freenove_panel_i2c_read(i2c, FPN_REG_ID, &data) == 0)
		dev_info(&i2c->dev, "panel MCU hw id = 0x%x\n", data);
	if (freenove_panel_i2c_read(i2c, FPN_REG_SIZE, &data) == 0)
		dev_info(&i2c->dev, "panel size = %d\n", data);
	if (freenove_panel_i2c_read(i2c, FPN_REG_VERSION, &data) == 0)
		dev_info(&i2c->dev, "panel MCU firmware version = 0x%x\n",
			 data);

	/*
	 * VCC plus IOVCC from the very start: IOVCC (bit 4) also feeds the
	 * GT911 touch, so raising it here means the chip is powered and has
	 * settled long before the goodix driver probes for it, instead of
	 * waiting for the panel's prepare(). The glass itself still needs
	 * AVDD + reset + DCS init, so bringing IOVCC up early neither lights
	 * it nor breaks the panel supply order. pre-power-on additionally
	 * fronts the analog rail and reset, needing a longer settle time.
	 */
	pre_power_on = device_property_read_bool(&i2c->dev, "pre-power-on");
	state->poweron_state = FPN_BIT_VCC | FPN_BIT_IOVCC;
	if (pre_power_on)
		state->poweron_state |= FPN_BIT_LCD_RESET | FPN_BIT_AVDD;
	freenove_panel_write_state(state, state->poweron_state);
	msleep(pre_power_on ? 200 : 20);

	/* describe the mask as GPIO lines; numbering is done by the core */
	state->gc.parent = &i2c->dev;
	state->gc.label = i2c->name;
	state->gc.owner = THIS_MODULE;
	state->gc.base = -1;			/* -1 asks for a dynamically allocated base */
	state->gc.ngpio = FPN_NUM_GPIO;
	state->gc.can_sleep = true;		/* the accessors block, consumers must be allowed to sleep */
	state->gc.get = freenove_panel_gpio_get;
	state->gc.set = freenove_panel_gpio_set;
	state->gc.direction_input = freenove_panel_gpio_direction_in;
	state->gc.direction_output = freenove_panel_gpio_direction_out;
	state->gc.get_direction = freenove_panel_gpio_get_direction;

	ret = devm_gpiochip_add_data(&i2c->dev, &state->gc, state);
	if (ret)
		return dev_err_probe(&i2c->dev, ret,
				     "failed to create gpiochip\n");

	/*
	 * The optional gate is bit 2 of the chip registered just above, so
	 * this lookup has to follow it. Starting low keeps the backlight off
	 * until update_status() sees a non-zero brightness.
	 */
	state->enable = devm_gpiod_get_optional(&i2c->dev, "enable",
						GPIOD_OUT_LOW);
	if (IS_ERR(state->enable))
		return dev_err_probe(&i2c->dev, PTR_ERR(state->enable),
				     "couldn't get our enable GPIO\n");

	/* the panel driver reaches this device through its DT backlight phandle */
	bl = devm_backlight_device_register(&i2c->dev, dev_name(&i2c->dev),
					    &i2c->dev, state,
					    &freenove_panel_bl_ops, &props);
	if (IS_ERR(bl))
		return PTR_ERR(bl);
	bl->props.brightness = 255;		/* come up at full brightness */

	/*
	 * Offer bit 4 as the "vddio" supply. The touch node references this
	 * node through power-supply / vddio-supply, so the kernel holds the
	 * goodix probe at bay (EPROBE_DEFER) until this line exists and
	 * IOVCC is genuinely on. Registered last, so the gpio_chip and the
	 * shadow mask are ready before anything can toggle the rail through
	 * the regulator.
	 */
	{
		struct regulator_config config = { };

		config.dev = &i2c->dev;
		config.of_node = i2c->dev.of_node;
		config.driver_data = state;

		state->iovcc_rdev = devm_regulator_register(&i2c->dev,
					  &freenove_panel_iovcc_desc, &config);
		if (IS_ERR(state->iovcc_rdev))
			return dev_err_probe(&i2c->dev,
					     PTR_ERR(state->iovcc_rdev),
					     "failed to register IOVCC regulator\n");
	}

	dev_info(&i2c->dev, "panel MCU probed: gpiochip + backlight + vddio registered\n");
	return 0;
}

/* Only the mutexes are ours to undo; regmap, the gpio_chip and the backlight are devm-managed. */
static void freenove_panel_i2c_remove(struct i2c_client *client)
{
	struct freenove_panel_mcu *state = i2c_get_clientdata(client);

	mutex_destroy(&state->dir_lock);
	mutex_destroy(&state->pwr_lock);
}

/* Kill the backlight before reboot, so the panel cannot flash while the display stack is going away. */
static void freenove_panel_i2c_shutdown(struct i2c_client *client)
{
	struct freenove_panel_mcu *state = i2c_get_clientdata(client);

	regmap_write(state->regmap, FPN_REG_PWM, 0);
}

/*
 * The string has to be the same as the compatible of panel-mcu@45 in
 * vc4-kms-dsi-freenove-panel.dts, otherwise nothing here is ever probed.
 */
static const struct of_device_id freenove_panel_dt_ids[] = {
	{ .compatible = "freenove,panel-mcu" },
	{ }
};
MODULE_DEVICE_TABLE(of, freenove_panel_dt_ids);

/* I2C driver object; module_i2c_driver() supplies the init/exit boilerplate. */
static struct i2c_driver freenove_panel_regulator_driver = {
	.driver = {
		.name = "freenove-panel-mcu",
		.of_match_table = freenove_panel_dt_ids,
	},
	.probe = freenove_panel_i2c_probe,
	.remove = freenove_panel_i2c_remove,
	.shutdown = freenove_panel_i2c_shutdown,
};
module_i2c_driver(freenove_panel_regulator_driver);

MODULE_AUTHOR("Ling Lin");
MODULE_DESCRIPTION("I2C panel MCU driver for the Freenove 7\" DSI panel");
MODULE_LICENSE("GPL");
