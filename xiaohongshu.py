from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re

from PIL import Image


BEIJING = timezone(timedelta(hours=8))
DATE_FORMAT = "%Y-%m-%dT%H:%M"
IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
IMAGE_LIMIT = 18
BYTE_LIMIT = 32_000_000
DECLARATIONS = {
    "ai": "笔记含AI合成内容",
    "repost": "来源转载",
    "self_shot": "自主拍摄",
    "fiction": "虚构演绎，仅供娱乐",
    "advertising": "内容包含营销广告",
}


def schedule_time(value, now=None):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        raise ValueError("请选择北京时间，精确到分钟。")
    try:
        date = datetime.strptime(value, DATE_FORMAT).replace(tzinfo=BEIJING)
    except ValueError as exc:
        raise ValueError("定时日期或时间无效。") from exc
    delta = date - (now or datetime.now(BEIJING))
    if not timedelta(hours=1) <= delta <= timedelta(days=14):
        raise ValueError("小红书支持在未来 1 小时至 14 天内定时发布（北京时间）。")
    return date


def image_info(source, name):
    expected = IMAGE_FORMATS.get(Path(name).suffix.lower())
    if expected is None:
        raise ValueError(f"小红书支持 JPG、JPEG、PNG 或 WebP 图片：{name}")
    if isinstance(source, Path):
        size = source.stat().st_size
    else:
        position = source.tell()
        source.seek(0, 2)
        size = source.tell()
        source.seek(position)
    if not 0 < size <= BYTE_LIMIT:
        raise ValueError(f"小红书单张图片最大 32 MB：{name}")
    try:
        with Image.open(source, formats=[expected]) as image:
            if getattr(image, "is_animated", False):
                raise ValueError("小红书图文请使用静态图片。")
            width, height = image.size
            image.verify()
        if hasattr(source, "seek"):
            source.seek(0)
        with Image.open(source, formats=[expected]) as image:
            image.load()
    except (OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ValueError(f"图片无法完整解码，或内容与扩展名不符：{name}") from exc
    return {"width": width, "height": height}


def post_options(raw):
    options = {}
    if raw.get("schedule") is not None:
        options["schedule"] = schedule_time(raw["schedule"]).strftime(DATE_FORMAT)
    declaration = raw.get("declaration")
    if declaration not in (None, ""):
        if not isinstance(declaration, str) or declaration not in DECLARATIONS:
            raise ValueError("请选择有效的小红书内容类型声明。")
        options["declaration"] = declaration
    source_name = raw.get("source_name", "")
    if not isinstance(source_name, str):
        raise ValueError("转载媒体名称须为文字。")
    source_name = source_name.strip()
    if declaration == "repost":
        if not source_name:
            raise ValueError("来源转载需要填写媒体名称。")
        options["source_name"] = source_name
    elif source_name:
        raise ValueError("媒体名称仅用于来源转载。")
    shoot_place = raw.get("shoot_place", "")
    shoot_address = raw.get("shoot_address", "")
    shoot_date = raw.get("shoot_date", "")
    if not all(isinstance(value, str) for value in (shoot_place, shoot_address, shoot_date)):
        raise ValueError("拍摄地点、地址和日期须为文字。")
    if declaration == "self_shot":
        if not shoot_place.strip() or len(shoot_place.strip()) > 100 or len(shoot_address.strip()) > 200:
            raise ValueError("自主拍摄需要填写地点名称，名称不超过 100 字。")
        try:
            selected_date = date.fromisoformat(shoot_date)
        except ValueError as exc:
            raise ValueError("请选择有效的拍摄日期。") from exc
        if selected_date > datetime.now(BEIJING).date():
            raise ValueError("拍摄日期不能晚于今天。")
        options.update(shoot_place=shoot_place.strip(), shoot_date=selected_date.isoformat())
        if shoot_address.strip():
            options["shoot_address"] = shoot_address.strip()
    elif shoot_place or shoot_address or shoot_date:
        raise ValueError("拍摄地点和日期仅用于自主拍摄声明。")
    original = raw.get("original", False)
    if type(original) is not bool:
        raise ValueError("原创确认须为 true 或 false。")
    if original:
        if declaration == "repost":
            raise ValueError("来源转载不能同时声明原创。")
        options["original"] = True
    return options
