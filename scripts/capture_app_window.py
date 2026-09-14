"""真实截取 AI 3D Forge **自己的原生窗口**（PrintWindow），用于视觉验收。

为什么要专门写这一支：这个会话本身的 DSH GUI 占着整个屏幕，
用「截全屏再裁」的办法既不可靠、也分不清截到的是谁。
PrintWindow 直接向目标窗口要一份绘制结果，不受遮挡影响。

用法：
    & build\\venv\\Scripts\\python.exe scripts\\capture_app_window.py <输出png> [--restore]

输出：PNG 路径 + 窗口矩形，供后续 vision 检查使用。
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

TITLE = "AI 3D Forge"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 0x00000002
SW_RESTORE = 9
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def find_window(title_part: str) -> int | None:
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_part in buf.value:
            found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    return found[0] if found else None


def write_png(path: Path, width: int, height: int, bgra: bytes) -> None:
    """把 BGRA 像素写成 PNG。

    为什么手写而不是用 Pillow：本项目刻意不引入图像库（依赖越少越好，
    打包体积也小）。PNG 的 zlib+CRC 结构用标准库几十行就能搞定。
    """
    import struct
    import zlib

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # 每行的过滤器类型：None
        row = bgra[y * stride : (y + 1) * stride]
        # BGRA -> RGB
        raw.extend(
            bytes(
                component
                for x in range(width)
                for component in (row[x * 4 + 2], row[x * 4 + 1], row[x * 4])
            )
        )

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def capture(hwnd: int, out_path: Path) -> tuple[int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)

    # PW_RENDERFULLCONTENT 才能拿到 WebView2 这种 GPU 合成的内容
    ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
    if not ok:
        ok = user32.PrintWindow(hwnd, mem_dc, 0)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # 负数 = 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0  # BI_RGB

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_png(out_path, width, height, buffer.raw)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)
    return width, height


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/work/app-window.png")
    hwnd = find_window(TITLE)
    if hwnd is None:
        print(f"[FAIL] 没找到标题包含 {TITLE!r} 的窗口")
        return 2
    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.6)
    width, height = capture(hwnd, out)
    print(f"[OK] 已截取窗口 hwnd={hwnd} 尺寸={width}x{height}")
    print(f"     输出：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
