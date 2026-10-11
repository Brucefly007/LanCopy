# LanCopy（局域网复制）

不依赖 VMware Tools，在同一局域网内（典型：宿主机 ↔ 虚拟机）同步剪贴板文字，并互传文件。

## 功能

- 打开后 **UDP 自动发现** 同伴并连接（一般不用填 IP）
- 文字：一端 `Ctrl+C`，另一端 `Ctrl+V`
- 文件：一端复制文件，另一端粘贴（接收目录默认可配置）
- 系统托盘：关闭/最小化进入托盘；绿=已连接，橙=发现中，灰=已停止
- 默认端口：TCP `3721`（数据）、UDP `3722`（发现）

## 快速使用（Windows 成品）

当前仓库与 **v0.1.0** 只发布源码和文档。Windows 文件夹版（解压后运行 `局域网复制.exe`）体积较大，可在后续 Release 中附加；本仓库不预置数百 MB 的成品。

1. 下载后续 Release 里的文件夹压缩包，解压后运行 `局域网复制.exe`
2. 虚拟机与宿主机各开一份（网卡需互通，NAT/Host-Only/桥接均可）
3. 等待状态变为「已连接」后即可复制粘贴

若发现成功但连不上：防火墙放行 **TCP 3721** 与 **UDP 3722**。

## 从源码运行

需要 Python 3.11+（Windows 推荐）。

```bash
pip install pywin32 pystray Pillow windnd
python lan_copy.py
```

打包（onedir）：

```bash
pip install pyinstaller pywin32 pystray Pillow windnd
pyinstaller -D -w -n 局域网复制 --hidden-import=win32timezone lan_copy.py
```

也可直接运行仓库里的 `build_exe.bat`。

## 协议概要

- 发现：UDP 广播 `lan_copy_hello`（含设备 id / 主机名 / tcp_port）
- 数据：TCP，4 字节大端长度前缀 + UTF-8 JSON（文件内容分块传输）
- 双端同时发现时，由 **id 字典序较小的一方** 主动建立 TCP，避免双连接

## 许可证

MIT
