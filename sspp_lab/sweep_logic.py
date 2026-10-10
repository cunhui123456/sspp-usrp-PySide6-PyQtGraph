"""兼容历史导入路径；当前实现按实验分类管理。"""
import sys
from importlib import import_module
_impl = import_module("sspp_experiments.material_sweep.logic")
if __name__ == "__main__":
    raise SystemExit(_impl.main())
else:
    sys.modules[__name__] = _impl
