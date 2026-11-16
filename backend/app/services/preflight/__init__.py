"""服务端重新检查清单，防止绕过浏览器提交危险路径或遗漏提醒。"""
import re
from app.schemas.upload import Issue, ManifestEntry
from app.services.nas.paths import safe_join
from app.services.nas.errors import NasPermissionDenied

REQUIRED = ["01定稿文件", "02设计方案", "03拍摄照片及视频", "04授权文件", "05 详情页"]
RESERVED = {"套装档案", "单品档案", "_历史版本", "_项目档案.txt"}


def ignored(entry: ManifestEntry) -> bool:
    name = entry.rel_path.split("/")[-1]
    return not entry.is_dir and (name.lower() in {"thumbs.db", ".ds_store", "desktop.ini"} or name.startswith("~$"))


def check(folder: str, files: list[ManifestEntry], exists: bool = False, prefix_length: int = 0) -> list[Issue]:
    issues: list[Issue] = []
    def add(code: str, level: str, path: str, message: str) -> None:
        issues.append(Issue(rule_code=code, level=level, path=path, message=message))
    parts = folder.split("_")
    if len(parts) != 3 or any(not value for value in parts):
        add("P01", "block", folder, "项目文件夹名必须为 编号_企业_项目名 三段")
    if exists:
        add("P02", "block", folder, "该项目已入库，补充功能尚未开放")
    seen: set[str] = set()
    kinds: dict[str, bool] = {}
    for path, entry in [(folder, None), *((item.rel_path, item) for item in files)]:
        if entry is not None and ignored(entry):
            add("P12", "ignore", path, "系统垃圾文件不上传")
            continue
        try:
            safe_join(path)
            if entry is None and "/" in path:
                raise NasPermissionDenied("根名称非法")
            if "\\" in path:
                raise NasPermissionDenied("目录分隔符只允许正斜线")
        except NasPermissionDenied:
            add("P03", "block", path, "名称含非法字符或不安全的路径")
        if prefix_length + len(folder if entry is None else folder + "/" + path) > 200:
            add("P04", "block", path, "项目内完整路径超过 200 字符")
        if any(part.casefold() in {value.casefold() for value in RESERVED} for part in path.split("/")):
            add("P05", "block", path, "出现系统保留名称")
        if entry is not None:
            key = path.casefold()
            if key in seen:
                add("manifest", "block", path, "清单中重复的目标路径")
            seen.add(key)
            kinds[key] = entry.is_dir
            nodes = path.split("/")
            if nodes[0] in ("套装", "单品") and ((entry.is_dir and len(nodes) > 1) or
                (not entry.is_dir and (len(nodes) != 2 or nodes[-1].split(".")[-1].lower() not in ("jpg", "jpeg", "png")))):
                add("P06", "block", path, "套装/单品中只能平铺 jpg、jpeg、png 图片")
    for path, is_dir in kinds.items():
        if not is_dir and any(other.startswith(path + "/") for other in kinds):
            add("manifest", "block", path, "文件与目录路径发生冲突")
    active = [entry for entry in files if not ignored(entry)]
    images = [item for item in active if not item.is_dir and item.rel_path.split("/")[0] in ("套装", "单品") and len(item.rel_path.split("/")) == 2]
    if not images:
        add("P07", "warn", "套装/单品", "套装和单品均为空")
    roots = {item.rel_path.split("/")[0] for item in active if item.is_dir or "/" in item.rel_path}
    for name in REQUIRED:
        if name not in roots:
            add("P08", "warn", name, "缺少资料夹：" + name)
    if len(parts) == 3 and re.search(r"[·—\s\uFF00-\uFFEF\u3000-\u303F]", parts[2]):
        add("P09", "warn", folder, "项目名包含不建议的空格或标点")
    return issues
