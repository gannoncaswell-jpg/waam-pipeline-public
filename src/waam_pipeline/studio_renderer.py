from functools import lru_cache
from pathlib import Path


_HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def renderer_source() -> str:

    three = (_HERE / "_vendor" / "three-r180.cjs").read_text(encoding="utf-8")
    renderer = (_HERE / "studio-renderer.js").read_text(encoding="utf-8")
    source = (
        "window.WaamThree = (function () {\n  const exports = {};\n"
        + three
        + "\n  return exports;\n})();\n"
        + renderer
    )

    return source.replace("</script", "<\\/script")
