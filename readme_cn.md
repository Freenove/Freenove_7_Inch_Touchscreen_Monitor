# Freenove 7inch-C DSI 屏 —— 树莓派 5 驱动

Read this in other languages: [English](readme.md) | [简体中文](readme_cn.md)

Freenove "7inch C" MIPI-DSI 触摸屏（EK79007 玻璃，1024x600，GT911 触摸）在
**树莓派 5** 上的驱动。该屏由两个内核模块（一个 DRM panel 驱动 + 一个屏上 I2C
MCU 驱动，负责电源/复位/背光）加一个设备树 overlay 驱动。

要求：树莓派 5，**64 位** Raspberry Pi OS（Bookworm 6.12 或 Trixie 6.18，均已验证）。
32 位镜像在 Pi 5 上虽也引导 arm64 内核，但无法编译 arm64 模块，所以请用 64 位系统。

本仓库把驱动分成两个目录，对应两种安装方式：

| 目录 | 方式 | 说明 |
|---|---|---|
| `manual_install/` | **手动 / 树外** | `install.py` 在树莓派上现场编译模块 + overlay 并部署，改 `config.txt`，附带背光滑条与触摸兜底。内核升级后需重跑。 |
| `normal_install/` | **内核内 / 自动** | 同一套驱动，按 `raspberrypi/linux` 的 in-tree 结构摆放，含 DT binding、`setup.py` 集成脚本和"编译→检查→提交上游"的手把手指南。合入后驱动随内核发布，一行 `dtoverlay=` 即可启用——用户无需编译，32/64 位镜像通吃。 |

## 快速开始

树莓派上手动（树外）安装：
```bash
cd manual_install
sudo python3 install.py
```

内核内 / 提交上游：见 `normal_install/readme_cn.md`。

## 文档

- `manual_install/readme.md` / `manual_install/readme_cn.md` —— 手动安装器用法（英/中）。
- `normal_install/readme.md` / `normal_install/readme_cn.md` —— 集成、编译、检查并提交到官方内核的手把手指南（英/中）。
