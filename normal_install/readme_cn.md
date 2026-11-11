# normal_install —— 把 Freenove 7inch-C DSI 屏驱动提交进官方内核

Read this in other languages: [English](readme.md) | [简体中文](readme_cn.md)

本目录是**内核 in-tree 版**：把两个驱动 + overlay + binding 合入
`raspberrypi/linux`，合入后用户只需一行 `dtoverlay=` 即可用屏，无需自行编译。
（自己用、不想提交，用同级 `manual_install/` 的 out-of-tree 方案即可。）

在树莓派上按下面步骤走一遍即可完成下载、编译、检查、提交。介绍从简，命令可直接复制。

## 目录内容

```
normal_install/
├── setup.py                                   # 一键把下面的文件集成进内核树（幂等）
├── drivers/
│   ├── gpu/drm/panel/panel-freenove-ek79007.c     # DRM panel 驱动
│   └── regulator/freenove-panel-regulator.c        # 屏上 I2C MCU（GPIO/regulator/背光）
├── arch/arm/boot/dts/overlays/
│   └── vc4-kms-dsi-freenove-panel-overlay.dts      # 设备树 overlay
└── Documentation/devicetree/bindings/
    ├── display/panel/freenove,ek79007-7inch-c.yaml # panel 绑定文档
    └── regulator/freenove,panel-mcu.yaml           # MCU 绑定文档
```

## 前置条件

- 树莓派 5，**64 位** Raspberry Pi OS（Bookworm 6.12 / Trixie 6.18 均已验证）。
- 能联网（apt、pip、git clone 都要）。

## 步骤 1：装构建依赖

```bash
sudo apt update
sudo apt install -y git build-essential bc bison flex libssl-dev \
    libncurses-dev device-tree-compiler rsync swig python3-dev python3-venv
```

## 步骤 2：（可选）克隆内核树

> `setup.py` 在 `~/linux` 不存在时会自动克隆，所以这一步可跳过；仅当你想指定分支/位置时才手动做。

```bash
cd ~
uname -r                       # 看内核大版本：6.12→rpi-6.12.y，6.18→rpi-6.18.y
git clone --depth 1 -b rpi-6.18.y https://github.com/raspberrypi/linux.git
cd linux
make kernelversion             # 记下版本号，后面 set 回调签名要对得上
```

> `gpio_chip->set` 的返回类型随内核版本变：≤6.8 是 `int`、6.9~6.16 是 `void`、
> ≥6.17 又是 `int`。本目录源码按**当前主线（≥6.17，`int`）**写死；若你在 6.9~6.16
> 的树上编译报 `incompatible-pointer-types`，把 `freenove_panel_gpio_set` 的
> `int`/`return -EINVAL`/`return 0` 改成 `void`/`return;`（去掉末尾 return）即可。

## 步骤 3：把 normal_install 传到 Pi，并集成进内核树

先在 Windows 上把整个 `normal_install/` 拷到 Pi（任选其一）：

```bash
# 方式 A：scp（在 Windows PowerShell 里执行，注意改成你的 Pi IP）
scp -r "normal_install" pi@<Pi的IP>:~/Freenove_7_Inch_Touchscreen_Monitor/

# 方式 B：如果 Pi 上已有本仓库，git pull 后即为最新
```

然后在 Pi 上跑 `setup.py`：若 `~/linux` 不存在会自动克隆，然后拷贝源文件 + 登记 vendor-prefix/Kconfig/Makefile/overlays：

```bash
python3 ~/Freenove_7_Inch_Touchscreen_Monitor/normal_install/setup.py
# 或指定其它内核树：python3 .../setup.py /home/pi/linux
```

看到一串 `[copy] ... / [reg] ...` 即完成。脚本幂等，可反复运行。

## 步骤 4：配置并编译

```bash
cd ~/linux
make bcm2712_defconfig                    # Pi 5；无此 target 就用 make bcm2711_defconfig
./scripts/config --module DRM_PANEL_FREENOVE_EK79007
./scripts/config --module REGULATOR_FREENOVE_PANEL
make olddefconfig
grep -E 'DRM_PANEL_FREENOVE_EK79007|REGULATOR_FREENOVE_PANEL' .config   # 两项都应是 =m

make -j$(nproc) Image modules dtbs
find . -name 'panel-freenove-ek79007.ko' -o \
       -name 'freenove-panel-regulator.ko' -o \
       -name 'vc4-kms-dsi-freenove-panel.dtbo'                          # 三个都在=成功
```

> 别用 `make .../xxx.ko` 或单个 `.dtbo` target：前者缺 `vmlinux.o` 会让 modpost 报一堆
> undefined symbol，后者在 arm64 下路径会拼错。统一用 `make Image modules dtbs`。

## 步骤 5：提交前检查

**5.1 代码风格**（应 0 errors / 0 warnings / 0 checks）：
```bash
cd ~/linux
scripts/checkpatch.pl --strict --file drivers/gpu/drm/panel/panel-freenove-ek79007.c
scripts/checkpatch.pl --strict --file drivers/regulator/freenove-panel-regulator.c
```

**5.2 绑定文档校验**（需 dtschema，装进虚拟环境）：
```bash
python3 -m venv ~/dtsenv
source ~/dtsenv/bin/activate
pip install --retries 10 --timeout 120 dtschema yamllint   # 若超时，多跑几次或加 --index-url https://pypi.org/simple

cd ~/linux
make dt_binding_check \
  DT_SCHEMA_FILES=Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml
make dt_binding_check \
  DT_SCHEMA_FILES=Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
```

> 输出里 `brcm,stb-pcie.yaml ... missing type definition` 之类是**别的内核自带 schema**
> 与 dtschema 版本不匹配产生的噪音，不是我们的文件报错，忽略即可。

## 步骤 6：确认真实邮箱

两个 yaml 的 maintainers 已是 `Zhentao-Lin <173039613@qq.com>`，确认与下一步 `git config` 一致：
```bash
cd ~/linux
nano Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml
nano Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
```

## 步骤 7：分 commit 并发起 Pull Request

```bash
cd ~/linux
git config user.name  "你的名字"
git config user.email "你的真实邮箱"

git add Documentation/devicetree/bindings/vendor-prefixes.yaml
git commit -s -m "dt-bindings: Add freenove vendor prefix"

git add Documentation/devicetree/bindings/display/panel/freenove,ek79007-7inch-c.yaml \
        Documentation/devicetree/bindings/regulator/freenove,panel-mcu.yaml
git commit -s -m "dt-bindings: Add bindings for the Freenove 7inch-C DSI panel"

git add drivers/gpu/drm/panel/panel-freenove-ek79007.c \
        drivers/gpu/drm/panel/Kconfig drivers/gpu/drm/panel/Makefile
git commit -s -m "drm/panel: ek79007: Add support for the Freenove 7inch-C panel"

git add drivers/regulator/freenove-panel-regulator.c \
        drivers/regulator/Kconfig drivers/regulator/Makefile
git commit -s -m "regulator: Add Freenove DSI panel MCU regulator/backlight driver"

git add arch/arm/boot/dts/overlays/vc4-kms-dsi-freenove-panel-overlay.dts \
        arch/arm/boot/dts/overlays/Makefile
git commit -s -m "ARM: dts: overlays: Add vc4-kms-dsi-freenove-panel"
```

每个 commit 正文补：硬件背景、双镜像测试结论、以及回应两个高频质疑——
**为何不并入 `panel-waveshare-dsi`**（同颗 I2C 0x45 MCU）、**为何不拆成 MFD**。

推到你自己 fork 并发 PR：
```bash
# 先在 GitHub 上 fork raspberrypi/linux
git remote add mine https://github.com/<你的账号>/linux.git
git push mine HEAD:refs/heads/freenove-dsi-panel
# 到 GitHub 网页：从 mine/freenove-dsi-panel → raspberrypi/linux 对应分支发 Pull Request
```

## 附：真机运行验证（可选）

功能层面你已用外层 out-of-tree 驱动在双镜像验证过；若要在自编内核里再验一次，
**用备用 SD 卡**（有变砖风险）：
```bash
sudo make modules_install
sudo cp arch/arm64/boot/Image.gz /boot/firmware/kernel8.img
find . -name 'vc4-kms-dsi-freenove-panel.dtbo' -exec sudo cp -v {} /boot/firmware/overlays/ \;
# config.txt 加：
#   dtoverlay=vc4-kms-v3d
#   dtoverlay=vc4-kms-dsi-freenove-panel,dsi1
sudo reboot
dmesg | grep -iE 'dsi|panel|freenove|goodix'
```

## 附：常见坑速查

| 现象 | 原因 / 解决 |
|---|---|
| `make defconfig` 报 recursive dependency | Kconfig 里 `BACKLIGHT_CLASS_DEVICE`/`GPIOLIB` 用了 `select`，改 `depends on`（setup.py 已按此写） |
| config 项在 menuconfig 里找不到 | 插到了 `endmenu` 之外；setup.py 已插到 `endmenu` 之前 |
| `make .../foo.ko` 一堆 undefined symbol | 单编模块缺 vmlinux.o，改用 `make Image modules dtbs` |
| checkpatch 报 DOS line endings | 文件是 CRLF；Pi 上 `sed -i 's/\r$//' <文件>`（setup.py 写入用 LF） |
| `dt-doc-validate not found` | dtschema 没装或没在 PATH；`source ~/dtsenv/bin/activate` 后再 `make` |
| `->set` incompatible-pointer-types | 内核在 6.9~6.16（void），见步骤 2 的签名说明 |
