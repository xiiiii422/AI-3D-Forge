"""固定零件库：经过真实编译验证的参数化零件（见 catalog.py 的说明）。"""

from .catalog import (
    COMMON_FILTER_PITCH,
    FILTER_SIZES,
    LIBRARY_NAME,
    PARTS,
    PartParam,
    PartSpec,
    library_path,
    match,
    prompt_block,
    thread_sizes_in,
)

__all__ = [
    "COMMON_FILTER_PITCH",
    "FILTER_SIZES",
    "LIBRARY_NAME",
    "PARTS",
    "PartParam",
    "PartSpec",
    "library_path",
    "match",
    "prompt_block",
    "thread_sizes_in",
]
