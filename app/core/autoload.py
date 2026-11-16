"""Convention-based discovery so adding a module never requires editing main.py or cli.py."""

import importlib
import importlib.util
import pkgutil
from types import ModuleType


def load_submodules(package: ModuleType, submodule: str) -> list[ModuleType]:
    """Import ``<package>.<child>.<submodule>`` for every child package that has one.

    Children without the submodule are skipped. An import error inside an existing
    submodule propagates, so a broken module fails loudly at startup.
    """
    loaded = []
    for info in sorted(pkgutil.iter_modules(package.__path__, package.__name__ + "."), key=lambda i: i.name):
        if not info.ispkg:
            continue
        target = f"{info.name}.{submodule}"
        if importlib.util.find_spec(target) is not None:
            loaded.append(importlib.import_module(target))
    return loaded
