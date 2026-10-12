"""AI 3D Forge —— 3MF 导出（3D Printing Consortium 的 3MF 1.0 核心规范）。

为什么要多这一个格式（对标市面成熟产品的结果）：
  * STL 只有三角形，**不带单位**：切片软件按自己的默认单位解释，
    用户常见的翻车是"导入后大 10 倍/小 100 倍"，还得手动缩放。
  * 3MF 明确声明 `unit="millimeter"`，还能带 Title/材料/打印机等元数据，
    Bambu Studio、OrcaSlicer、PrusaSlicer、Cura、Creality Print 都直接吃它。
    市面 AI 3D 生成工具（Meshy 等）已经把它当作"可打印交付物"的标配。

为什么自己写而不再跑一次 OpenSCAD：
  * 再编译一次要付同样的时间（复杂模型几十秒），而切片要的就是**已经交付的那个 STL**；
    从 STL 直接转，保证 3MF 与用户下载的 STL 面数、几何完全一致。
  * 顺带可以把机型、材料、设计尺寸写进元数据，切片软件里一眼看得到。

只使用标准库（zipfile / xml 字符串拼接），不引入新依赖。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_3mf_bytes(
    triangles: list[tuple[float, float, float]],
    *,
    title: str = "Model",
    metadata: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, int]]:
    """把三角形列表打包成 3MF 字节流。返回 (文件内容, {triangles, vertices})。

    顶点按 1e-5mm 量化去重（STL 里每个角都重复存一份坐标，去重后体积通常小一半以上）。
    """
    if len(triangles) % 3 != 0:
        raise ValueError(f"顶点数不是 3 的倍数：{len(triangles)}")

    vertex_index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    indices: list[int] = []
    for point in triangles:
        key = (round(point[0], 5), round(point[1], 5), round(point[2], 5))
        found = vertex_index.get(key)
        if found is None:
            found = len(vertices)
            vertex_index[key] = found
            vertices.append((float(point[0]), float(point[1]), float(point[2])))
        indices.append(found)

    # <model> 的子元素**顺序与形状都是规范强制的**：
    #   <model> → <metadata name="…">* → <resources> → <build>
    # 这个形状踩过两次，两次都只在「用户能打开吗」验不到的地方：
    #   1. 顺序写成 resources → metadata → build；
    #   2. 把所有 <metadata name=…> 包进一层**没有 name 的 <metadata>**。规范里没有
    #      这种包裹元素：`name` 是 metadata 的必需属性，metadata 也不允许再套子元素。
    #      官方样例就是 <model unit="millimeter" …><metadata name="Copyright">…</metadata><resources>。
    # 多数切片软件（lib3mf 按元素名取子元素）对顺序与多余层级宽容，所以
    # 「某个切片软件打不开、另一个能开」这种偶发问题极难定位；严格校验器与
    # 3MF 官方 Compliance Tool 会直接判不合格。
    meta_lines: list[str] = []
    seen: set[str] = set()
    for name, value in (metadata or {}).items():
        if value is None or str(value) == "":
            continue
        meta_lines.append(f'<metadata name="{_esc(name)}">{_esc(value)}</metadata>')
        seen.add(name)
    if "Title" not in seen:
        meta_lines.insert(0, f'<metadata name="Title">{_esc(title)}</metadata>')

    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}">',
    ]
    # metadata 必须直接挂在 <model> 下（见上面的规范说明），再才是 resources 与 build
    parts.extend(meta_lines)
    parts.append("<resources>")
    parts.append(f'<object id="1" type="model" name="{_esc(title)}">')
    parts.append("<mesh>")
    parts.append("<vertices>")
    parts.extend(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in vertices)
    parts.append("</vertices>")
    parts.append("<triangles>")
    for i in range(0, len(indices), 3):
        parts.append(f'<triangle v1="{indices[i]}" v2="{indices[i + 1]}" v3="{indices[i + 2]}"/>')
    parts.append("</triangles>")
    parts.append("</mesh>")
    parts.append("</object>")
    parts.append("</resources>")
    parts.append('<build><item objectid="1"/></build>')
    parts.append("</model>")
    model_xml = "\n".join(parts)

    # 用普通 BytesIO 再 getvalue()：拿 bytearray 共享缓冲区那个写法实测会写出不完整的包
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("3D/3dmodel.model", model_xml)
    data = stream.getvalue()
    return data, {"triangles": len(indices) // 3, "vertices": len(vertices)}


def write_3mf_from_stl(
    stl_path: Path,
    out_path: Path,
    *,
    title: str = "Model",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """从已生成的 STL 写出 3MF（切片软件用）。返回统计信息。

    调用方负责把它当"尽力而为"的附加产物：写失败不应影响主交付物 STL。
    """
    from .mesh import read_triangles  # 延迟导入，避免与 mesh 的循环依赖

    points, _is_ascii = read_triangles(stl_path)
    data, stats = build_3mf_bytes(points, title=title, metadata=metadata)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    stats["bytes"] = len(data)
    stats["path"] = str(out_path)
    return stats


def read_3mf_summary(path: Path) -> dict[str, Any]:
    """读回 3MF 的关键信息（自检与排障用）：单位、面数、顶点数、元数据。"""
    import re

    with zipfile.ZipFile(path) as archive:
        xml = archive.read("3D/3dmodel.model").decode("utf-8")
    unit_match = re.search(r'<model[^>]*\bunit="([^"]+)"', xml)
    metadata: dict[str, str] = {}
    for match in re.finditer(r'<metadata name="([^"]+)">([^<]*)</metadata>', xml):
        metadata[match.group(1)] = match.group(2)
    return {
        "unit": unit_match.group(1) if unit_match else None,
        "triangles": xml.count("<triangle "),
        "vertices": xml.count("<vertex "),
        "metadata": metadata,
        "has_build": "<build>" in xml,
        "object_name": (re.search(r'<object[^>]*name="([^"]*)"', xml) or [None, None])[1],
    }
