# LanCopy（局域网复制）

不依赖 VMware Tools，在局域网内（典型：**宿主机 ↔ VMware 虚拟机**）同步剪贴板文字与文件。

仓库：[https://github.com/Brucefly007/LanCopy](https://github.com/Brucefly007/LanCopy)

## 功能

- **自动发现**：打开后 UDP 广播找同伴，一般不用填 IP
- **文字**：一端 `Ctrl+C`，另一端立刻可 `Ctrl+V`
- **文件**：一端 `Ctrl+C` 只同步名单；对端点 **「拉取待传文件」**（窗口绿色按钮或**托盘右键**）才下载到「局域网接收」，并写入剪贴板，可再 `Ctrl+V`（避免无意复制误传）
- **大文件**：二进制分块（约 4MB）、边收边写磁盘、同名未传完可按偏移续传
- **系统托盘**：关闭/最小化进托盘；收到待传文件会气泡提示；绿=已连接，橙=发现中，灰=已停止
- 默认端口：TCP `3721`（数据）、UDP `3722`（发现）

## 快速使用（Windows）

1. 从 [Releases](https://github.com/Brucefly007/LanCopy/releases) 下载 **v0.3.0** 的 `LanCopy-windows-x64.zip`，解压
2. 虚拟机与宿主机各运行 `局域网复制.exe`（拷贝整个文件夹，不要只拷 exe）
3. 等待「已连接」后：
   - 复制文字 → 对面即可粘贴
   - 复制文件 → 对面日志/托盘提示「有待传文件」→ **点「拉取待传文件」** 后才开始下载 → 再 `Ctrl+V`

若发现成功但连不上：防火墙放行 **TCP 3721** 与 **UDP 3722**。

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

## 协议概要

| 通道 | 用途 |
|------|------|
| UDP `3722` | 广播 `lan_copy_hello`（设备 id / 主机名 / tcp_port） |
| TCP `3721` | 剪贴板与文件；帧 = 4 字节大端长度 + JSON；文件内容为原始字节块 |

- 双端同时发现时，**id 字典序较小的一方**主动连 TCP，避免双连接
- 文件：`file_offer`（仅名单）→ 对端手动拉取 → `file_request` / `bin_chunk` / `file_stream_done` → 写入真实 `CF_HDROP`

## 已知限制

- 同时只保持一条连接
- 口令已取消；请仅在可信局域网使用
- 文件不会因复制自动传输，必须对端主动点「拉取」
- 当前为单流 TCP（未做多连接并行窗口）

## 许可证

MIT
