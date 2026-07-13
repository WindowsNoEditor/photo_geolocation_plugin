# -*- coding: utf-8 -*-
"""照片定位识别插件 —— MaiBot 修补插件

群友发照片时，读取图片 EXIF 里的 GPS 定位，逆地理编码成中文地址，@ 发图人回复
「请问是在 <地址> 的 <昵称> 群友吗？」。

坐标说明：照片 EXIF 里的 GPS 是 WGS-84。OSM/Nominatim 直接用 WGS-84；高德用的是
GCJ-02（火星坐标），所以走高德前会先把坐标 WGS-84→GCJ-02 转换，避免偏移。

重要现实：QQ 发普通（压缩）图通常会抹掉 EXIF/GPS，一般只有发**原图或文件**才带 GPS，
所以本插件多数时候静默、只有真读到 GPS 才回复。MaiBot 自身不剥 EXIF（原样存盘）。

通过 chat.receive.before_process 钩子在最早阶段拿原始字节，不改动 MaiBot 核心源码。
"""

from __future__ import annotations

import asyncio
import base64
import glob
import io
import math
import os
from collections import deque
from time import monotonic
from typing import Any, Optional

import httpx

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

PLUGIN_VERSION = "1.0.0"

_GPS_IFD = 34853  # EXIF GPSInfo IFD tag
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff")


# ----------------------------- WGS-84 -> GCJ-02（高德用） -----------------------------
def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _tf_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _tf_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def _wgs84_to_gcj02(lat: float, lon: float) -> tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    a = 6378245.0
    ee = 0.00669342162296594323
    dlat = _tf_lat(lon - 105.0, lat - 35.0)
    dlon = _tf_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


class BasicSection(PluginConfigBase):
    __ui_label__ = "基础设置"
    config_version: str = Field(
        default=PLUGIN_VERSION, description="配置文件版本号，请勿手动修改",
        json_schema_extra={"label": "配置版本号"})
    enabled: bool = Field(
        default=True, description="总开关，关闭后不再识别照片定位",
        json_schema_extra={"label": "启用插件"})
    geocoder: str = Field(
        default="osm",
        description="逆地理编码服务：osm=免费无需key（Nominatim/OpenStreetMap）；amap=高德（需填 key，中国最准）。",
        json_schema_extra={"label": "地理编码服务(osm/amap)"})
    amap_key: str = Field(
        default="",
        description="高德 Web 服务 API Key（geocoder=amap 时必填；osm 不需要）。",
        json_schema_extra={"label": "高德Key"})
    amap_township_only: bool = Field(
        default=True,
        description="高德地址只显示到「市+区+街道」，不暴露具体小区/门牌（更含蓄）。关闭则显示完整地址。",
        json_schema_extra={"label": "地址只到街道"})
    reply_template: str = Field(
        default="请问是在 {address} 的 {nick} 群友吗？",
        description="回复模板，{address}=识别到的地址，{nick}=发图人昵称。",
        json_schema_extra={"label": "回复模板"})
    at_user: bool = Field(
        default=True, description="回复时先 @ 一下发图的群友。",
        json_schema_extra={"label": "回复时@对方"})
    group_only: bool = Field(
        default=True, description="只在群聊生效。",
        json_schema_extra={"label": "仅群聊生效"})
    scan_files: bool = Field(
        default=True, description="除了内联图片，也尝试读取以“文件”形式发送的图片（GPS 存活率更高）。",
        json_schema_extra={"label": "也读文件形式的图"})
    cooldown_seconds: float = Field(
        default=20.0, ge=0.0,
        description="同一个人两次定位回复之间至少间隔多少秒，防刷屏。",
        json_schema_extra={"label": "同一人冷却(秒)"})
    address_max_len: int = Field(
        default=60, ge=0,
        description="地址最长显示多少字，0 表示不截断。",
        json_schema_extra={"label": "地址最大长度"})
    osm_language: str = Field(
        default="zh", description="OSM 返回地址的语言。",
        json_schema_extra={"label": "OSM语言"})
    request_timeout: float = Field(
        default=12.0, ge=3.0, description="逆地理编码请求超时秒数。",
        json_schema_extra={"label": "请求超时(秒)"})
    images_dir: str = Field(
        default="/MaiMBot/data/images",
        description="收到的图片文件目录（按 hash 兜底读取）。一般无需修改。",
        json_schema_extra={"label": "图片目录"})


class GeoConfig(PluginConfigBase):
    plugin: BasicSection = Field(default_factory=BasicSection)


class PhotoGeoPlugin(MaiBotPlugin):
    config_model = GeoConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._done: deque = deque(maxlen=500)      # 已处理过的图片 hash，避免重复回复
        self._done_set: set = set()
        self._cooldown: dict[str, float] = {}      # (gid|uid) -> ts
        self._tasks: set = set()

    async def on_load(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self.config.plugin.request_timeout,
            headers={"User-Agent": "maibot-photo-geo/1.0 (QQ group bot)"},
            follow_redirects=True)
        self.ctx.logger.info("[照片定位] 插件已加载，地理编码=%s", self.config.plugin.geocoder)

    async def on_unload(self) -> None:
        for t in list(self._tasks):
            try:
                t.cancel()
            except Exception:
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self.ctx.logger.info("[照片定位] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if self._client is not None:
            try:
                self._client.timeout = httpx.Timeout(self.config.plugin.request_timeout)
            except Exception:
                pass
        self.ctx.logger.info("[照片定位] 配置已更新（%s）", scope)

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----------------------------- 取图片字节 -----------------------------
    def _bytes_from_component(self, comp: dict) -> tuple[Optional[bytes], str]:
        b64 = comp.get("binary_data_base64")
        if b64:
            try:
                return base64.b64decode(b64), str(comp.get("hash") or "")
            except Exception:
                pass
        h = str(comp.get("hash") or "").strip()
        if h:
            for p in glob.glob(os.path.join(self.config.plugin.images_dir, h + ".*")):
                try:
                    with open(p, "rb") as f:
                        return f.read(), h
                except Exception:
                    continue
        # “文件”形式：data 里可能带本地路径
        data = comp.get("data")
        if isinstance(data, dict):
            for key in ("path", "file", "file_path", "local_path", "url"):
                v = data.get(key)
                if isinstance(v, str) and os.path.isfile(v) and v.lower().endswith(_IMG_EXTS):
                    try:
                        with open(v, "rb") as f:
                            return f.read(), (h or os.path.basename(v))
                    except Exception:
                        continue
        return None, h

    def _candidates(self, raw: list) -> list[dict]:
        out = []
        for comp in raw:
            if not isinstance(comp, dict):
                continue
            t = comp.get("type")
            if t == "image":
                out.append(comp)
            elif t == "file" and self.config.plugin.scan_files:
                data = comp.get("data")
                name = ""
                if isinstance(data, dict):
                    name = str(data.get("name") or data.get("file") or data.get("path") or "")
                if (not name) or name.lower().endswith(_IMG_EXTS):
                    out.append(comp)
        return out

    # ----------------------------- EXIF GPS -----------------------------
    def _exif_gps(self, data: bytes) -> Optional[tuple[float, float]]:
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            exif = im.getexif()
            if not exif:
                return None
            gps = exif.get_ifd(_GPS_IFD)
            if not gps:
                return None
            lat = gps.get(2)
            lat_ref = gps.get(1)
            lon = gps.get(4)
            lon_ref = gps.get(3)
            if not lat or not lon:
                return None

            def dms(v):
                return float(v[0]) + float(v[1]) / 60.0 + float(v[2]) / 3600.0

            latitude = dms(lat)
            longitude = dms(lon)
            if str(lat_ref).upper().startswith("S"):
                latitude = -latitude
            if str(lon_ref).upper().startswith("W"):
                longitude = -longitude
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                return None
            if abs(latitude) < 1e-6 and abs(longitude) < 1e-6:
                return None
            return latitude, longitude
        except Exception:
            return None

    # ----------------------------- 逆地理编码 -----------------------------
    async def _reverse(self, lat: float, lon: float) -> Optional[str]:
        cfg = self.config.plugin
        if cfg.geocoder.strip().lower() == "amap" and cfg.amap_key.strip():
            addr = await self._reverse_amap(lat, lon)
            if addr:
                return addr
            # 高德失败则回退 OSM
        return await self._reverse_osm(lat, lon)

    async def _reverse_osm(self, lat: float, lon: float) -> Optional[str]:
        if self._client is None:
            return None
        try:
            r = await self._client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json",
                        "accept-language": self.config.plugin.osm_language or "zh", "zoom": 18})
            if r.status_code != 200:
                self.ctx.logger.warning("[照片定位] OSM HTTP %s", r.status_code)
                return None
            j = r.json()
            return (j.get("display_name") or "").strip() or None
        except Exception:
            self.ctx.logger.exception("[照片定位] OSM 逆地理失败")
            return None

    async def _reverse_amap(self, lat: float, lon: float) -> Optional[str]:
        if self._client is None:
            return None
        glat, glon = _wgs84_to_gcj02(lat, lon)
        try:
            r = await self._client.get(
                "https://restapi.amap.com/v3/geocode/regeo",
                params={"location": "%.6f,%.6f" % (glon, glat),
                        "key": self.config.plugin.amap_key.strip(), "extensions": "base"})
            j = r.json()
            if str(j.get("status")) != "1":
                self.ctx.logger.warning("[照片定位] 高德返回异常：%s", str(j)[:150])
                return None
            reg = j.get("regeocode") or {}
            if self.config.plugin.amap_township_only:
                ac = reg.get("addressComponent") or {}

                def _s(x):
                    return x.strip() if isinstance(x, str) and x.strip() else ""

                city = _s(ac.get("city")) or _s(ac.get("province"))  # 直辖市 city 为空，用 province
                addr = city + _s(ac.get("district")) + _s(ac.get("township"))
                if addr:
                    return addr
            addr = reg.get("formatted_address") or ""
            if isinstance(addr, list):
                addr = addr[0] if addr else ""
            return str(addr).strip() or None
        except Exception:
            self.ctx.logger.exception("[照片定位] 高德逆地理失败")
            return None

    # ----------------------------- 处理一张图 -----------------------------
    async def _handle_photo(self, data: bytes, img_hash: str, stream_id: str,
                            uid: str, nick: str, key: str) -> None:
        try:
            gps = self._exif_gps(data)
            if not gps:
                return
            lat, lon = gps
            addr = await self._reverse(lat, lon)
            if not addr:
                self.ctx.logger.info("[照片定位] 读到GPS(%.6f,%.6f)但逆地理无结果", lat, lon)
                return
            maxlen = self.config.plugin.address_max_len
            if maxlen and len(addr) > maxlen:
                addr = addr[:maxlen] + "…"
            cfg = self.config.plugin
            try:
                text = cfg.reply_template.format(address=addr, nick=nick or "这位")
            except Exception:
                text = "请问是在 %s 的 %s 群友吗？" % (addr, nick or "这位")
            if cfg.at_user and uid:
                try:
                    await self.ctx.send.custom("at", {"target_user_id": str(uid)}, stream_id)
                except Exception:
                    self.ctx.logger.exception("[照片定位] @对方失败")
            await self.ctx.send.text(text, stream_id)
            self._cooldown[key] = monotonic()
            self.ctx.logger.info("[照片定位] 命中 群友=%s GPS=%.6f,%.6f -> %s", nick or uid, lat, lon, addr)
        except Exception:
            self.ctx.logger.exception("[照片定位] 处理照片异常")

    # ----------------------------- 入站钩子 -----------------------------
    @HookHandler("chat.receive.before_process", name="photo_geo",
                 mode=HookMode.BLOCKING, order=HookOrder.LATE,
                 timeout_ms=15000, error_policy=ErrorPolicy.SKIP)
    async def handle(self, message: Optional[dict] = None, **kwargs: Any):
        try:
            cfg = self.config.plugin
            if not cfg.enabled or not isinstance(message, dict):
                return None
            if message.get("is_command") or message.get("is_notify"):
                return None
            raw = message.get("raw_message")
            if not isinstance(raw, list):
                return None

            info = message.get("message_info") if isinstance(message.get("message_info"), dict) else {}
            ginfo = info.get("group_info") if isinstance(info, dict) else None
            gid = str(ginfo.get("group_id") or "").strip() if isinstance(ginfo, dict) else ""
            if cfg.group_only and not gid:
                return None
            uinfo = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
            uid = str(uinfo.get("user_id") or "").strip()
            nick = str(uinfo.get("user_cardname") or uinfo.get("user_nickname") or "").strip()
            stream_id = str(message.get("session_id") or "").strip()
            if not stream_id:
                return None

            comps = self._candidates(raw)
            if not comps:
                return None

            # 冷却
            key = (gid or "-") + "|" + uid
            now = monotonic()
            last = self._cooldown.get(key, 0.0)
            if cfg.cooldown_seconds and last and now - last < cfg.cooldown_seconds:
                return None

            for comp in comps:
                data, h = self._bytes_from_component(comp)
                if not data:
                    continue
                if h and h in self._done_set:
                    continue
                if h:
                    self._done.append(h)
                    self._done_set = set(self._done)
                # 后台处理（读EXIF + 网络逆地理），不阻塞入站主链
                self._track(asyncio.ensure_future(
                    self._handle_photo(data, h, stream_id, uid, nick, key)))
                break  # 一条消息只处理第一张候选图，避免刷屏
        except Exception:
            self.ctx.logger.exception("[照片定位] 钩子异常")
        return None


def create_plugin() -> PhotoGeoPlugin:
    return PhotoGeoPlugin()
