# LanCopy（局域网复制）

不依赖 VMware Tools，在局域网内（典型：**宿主机 ↔ VMware 虚拟机**）同步剪贴板文字，并按需互传文件。

仓库：[https://github.com/Brucefly007/LanCopy](https://github.com/Brucefly007/LanCopy)

## 功能

- **自动发现**：打开后 UDP 广播找同伴，一般不用填 IP
- **文字**：一端 `Ctrl+C`，另一端立刻可 `Ctrl+V`
- **文件按需传**：一端 `Ctrl+C` 复制文件时只同步名单；到另一台真正 `Ctrl+V` 粘贴时才开始传内容
- **大文件**：二进制分块（约 4MB）、边收边写磁盘、同名未传完可按偏移续传
- **系统托盘**：关闭/最小化进托盘；绿=已连接，橙=发现中，灰=已停止
- 默认端口：TCP `3721`（数据）、UDP `3722`（发现）

## 快速使用（Windows）

v0.2.0 以源码发布，请按下一节运行或自行打包。已发布的 [v0.1.0 Windows 压缩包](https://github.com/Brucefly007/LanCopy/releases/tag/v0.1.0) 是上一版：复制文件后会直接互传，没有「粘贴才开始传内容」。

若已有本版打包出的文件夹：

1. 虚拟机与宿主机各运行 `局域网复制.exe`（保留整个文件夹，不要只拷 exe）
2. 等待「已连接」后：复制文字即时同步；复制文件后到对面粘贴再传

若发现成功但连不上：防火墙放行 **TCP 3721** 与 **UDP 3722**。

个别环境若「粘贴不触发传文件」，可用窗口里的「拉取待传文件」。

## 从源码运行

Python 3.11+（Windows）：

```bash
pip install pywin32 pystray Pillow windnd
python lan_copy.py
```

打包（文件夹 onedir）：

```bash
pip install pyinstaller pywin32 pystray Pillow windnd
pyinstaller -D -w -n 局域网复制 --hidden-import=win32timezone lan_copy.py
```

或运行 `build_exe.bat`（单文件 `LanCopy.exe`，复制到桌面）。上面的 `-D` 命令打出的是文件夹版 `局域网复制`，使用时请保留整个文件夹。

## 协议概要

| 通道 | 用途 |
|------|------|
| UDP `3722` | 广播 `lan_copy_hello`（设备 id / 主机名 / tcp_port） |
| TCP `3721` | 剪贴板与文件；帧 = 4 字节大端长度 + JSON；文件内容为原始字节块 |

- 双端同时发现时，**id 字典序较小的一方**主动连 TCP，避免双连接
- 文件：`file_offer` → 对端延迟 `CF_HDROP` → 粘贴时 `file_request`（可带偏移续传）/ `bin_chunk` / `file_stream_done`

## 已知限制

- 同时只保持一条连接
- 口令已取消；请仅在可信局域网使用
- 超大文件粘贴时，传完前资源管理器可能等待（窗口有进度）
- 当前为单流 TCP（未做多连接并行窗口）

## 许可证

MIT
