"""绿色金融能力凭证核验服务。"""

import importlib

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    # 惰性导入：迁移脚本只需 models，不应连带创建应用与默认数据库
    if name in __all__:
        module = importlib.import_module(".app", package=__name__)
        return getattr(module, name)
    raise AttributeError(name)
