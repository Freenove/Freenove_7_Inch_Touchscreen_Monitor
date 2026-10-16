# Freenove 7inch-C DSI 屏 —— `install.py` 使用说明

`install.py` 是在树莓派 5 上安装 Freenove 7-DSI-TOUCH-C 屏驱动（EK79007 玻璃，
1024x600）的一键前端脚本。

## 它做了什么

一次运行按顺序完成以下四步：

1. **型号检查** —— 本屏仅支持树莓派 5；其它型号会使脚本以退出码 `1` 停止。
2. **编译并安装** —— 编译两个内核模块与设备树 overlay 并部署。当 `drivers/build/`
   已完整且不过期时，会跳过编译、只做部署。
3. **config.txt** —— 询问屏线接在哪个 DSI 接口（`dsi0` / `dsi1`），并把
   `/boot/firmware/config.txt` 改成与之完全一致：注释掉错误或冲突的行，
   在文件末尾追加正确的行。
4. **亮度启动器** —— 在桌面用户的应用菜单里加入一个可点击启动的背光滑条入口。

脚本可重复运行（幂等），没有非交互模式：请在终端里运行并回答提示。

## 运行环境要求

- 树莓派 5，系统为 Raspberry Pi OS（Bookworm / Trixie）。**本屏仅支持树莓派 5
  （BCM2712）**——树莓派 3 / 4 无法驱动（原因见“注意事项”），`install.py` 在其它
  型号上会以退出码 `1` 停止。
- Python 3 —— 安装器本身只用标准库。
- root 权限 —— 用 `sudo` 运行。
- 它启动的背光滑条（`drivers/brightness.py`）需要 `python3-pyqt5`。

## 工程结构

一切都由留在项目根目录的 `install.py` 驱动，它调用的文件分为两个子目录：

```
Freenove_TouchScreen_Driver_Install/
├── install.py                         # 你运行的前端安装器
├── readme.md / readme_cn.md           # 使用文档
├── drivers/                           # build.sh 编译与部署的全部内容
│   ├── build.sh                       #   编译/部署脚本
│   ├── Makefile                       #   内核模块构建规则
│   ├── panel-freenove-ek79007.c        #   DRM 屏驱动源码
│   ├── freenove-panel-regulator.c      #   屏上 MCU（GPIO+背光）驱动源码
│   ├── vc4-kms-dsi-freenove-panel.dts  #   设备树 overlay 源码
│   ├── brightness.py                   #   背光滑条
│   └── build/                          #   （生成物）编译出的 .ko/.dtbo
└── logo/
    └── brightness.png                 # 菜单启动器的图标
```

`build.sh` 始终以自己所在目录为基准，所以无论从哪里调用，`build/` 都生成在
`drivers/build/`。`install.py` 会自动在这些子目录里查找源码、产物和启动器文件。

## 用法

```bash
sudo python3 install.py
```

## 命令行选项

### 安装流程

| 选项 | 作用 |
|---|---|
| *（不带选项）* | 完整交互安装：型号检查 → 编译/部署 → `config.txt` → 亮度启动器 → 总结 |
| `--reinstall` | 忽略 `drivers/build/` 是否新鲜的判断，强制完整“编译 + 部署” |
| `--rotation X` | 直接设置显示旋转（`0` / `90` / `180` / `270`） |
| `--swapxy` | 直接设置触摸 XY 交换 |
| `--invx` | 直接设置触摸 X 反转 |
| `--invy` | 直接设置触摸 Y 反转 |
| `--sizex N` / `--sizey N` | 直接设置触摸坐标范围 |

以上选项都只写入 `config.txt`（开机覆盖参数），**不修改 `.dts`、不触发重新编译**。

### 查询当前配置（只读）

`--screen` 和 `--touch` 是纯查询：它们从 `config.txt` 读取当前的 overlay
参数，对没有配置的地方显示 `.dts` 默认值，不改动任何内容就直接退出
（不做型号检查、不编译）。要修改某个值，请使用上面的独立开关
`--rotation` / `--swapxy` / `--invx` / `--invy` / `--sizex` / `--sizey`。

| 选项 | 打印内容 |
|---|---|
| `--screen` | 当前的显示旋转（`0` / `90` / `180` / `270`） |
| `--touch` | 当前触摸配置：`swapxy` / `invx` / `invy` 开关状态 + `sizex` / `sizey` 坐标范围 |

### 删除已安装的内容

下面每个选项只做对应的清理然后退出；`--uninstall` 会把它们全部执行。它们会
**立即生效**（没有预览模式）。

| 选项 | 删除内容 |
|---|---|
| `--clean` | 生成的 `drivers/build/` 与根目录的 `__pycache__` |
| `--remove-overlay` | `/boot/firmware/overlays/` 下的 `vc4-kms-dsi-freenove-panel.dtbo` |
| `--remove-config` | 本安装器加进 `config.txt` 的 Freenove `dtoverlay` 行（你原有的 `dtoverlay=vc4-kms-v3d` 不受影响） |
| `--remove-menu` | `~/.local/share/applications/` 下的亮度启动器及其副本文件 |
| `--uninstall` | 以上全部 |

## 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 硬件不支持（不是树莓派 5） |
| `2` | 出错 |
| `3` | 用户取消 |

## 注意事项

- **仅支持树莓派 5 —— Pi 4 为何黑屏** —— overlay 是按树莓派 5（BCM2712）的显示
  通路绑定本屏的：屏上 MCU / GT911 触摸挂到 `&i2c_csi_dsi0`，DSI 主机挂到 `&dsi0`。
  而树莓派 4（BCM2711）的 DSI 显示座、CSI 与 I2C 路由都不同，因此在该板上 I2C
  `0x45` 处的 MCU 根本不会出现，regulator / panel 驱动无节点可绑定，点亮屏幕所需的
  上电 + DCS 初始化时序也就跑不起来。这是**硬件 / 设备树层面的差异，换更新的系统镜像
  并不能解决**
- **重启生效** —— overlay 与 `config.txt` 的改动需 `sudo reboot` 后生效。
- **内核升级后** —— 执行 `apt full-upgrade` 后，已编译的模块与运行内核不再匹配，
  需重新运行 `sudo python3 install.py` 重新编译。
- **亮度启动器** —— 它以 `Terminal=false` 在后台启动 `brightness.py`。而
  `brightness.py` 通过 `sudo tee` 写背光，所以只有当 `sudo` 凭据已缓存、或该命令
  被配置为免密（NOPASSWD）时，点击才能真正改变亮度。
