"""跨平台评测用例转换器。

支持的来源：
- swe-bench: SWE-bench / SWE-bench-Lite → spec.yaml
"""

from agent_eval.converters.base import BaseConverter
from agent_eval.converters.swe_bench import SWEBenchConverter

CONVERTERS = {
    "swe-bench": SWEBenchConverter,
}


def get_converter(name: str) -> BaseConverter:
    """按名称获取转换器实例。"""
    if name not in CONVERTERS:
        raise ValueError(f"未知转换器: {name}，可用: {sorted(CONVERTERS)}")
    return CONVERTERS[name]()


__all__ = ["BaseConverter", "SWEBenchConverter", "CONVERTERS", "get_converter"]
