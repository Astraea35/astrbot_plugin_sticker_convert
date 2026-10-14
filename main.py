import os
import sqlite3
import hashlib
import aiohttp
import asyncio
import re
import base64
import time
from io import BytesIO
from pathlib import Path
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, Reply, File
from astrbot.api import logger

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

@register("sticker_convert", "Astraea35", "QQ表情转存与文件提取插件", "1.1.0", "将QQ表情转存并以纯文件格式发送，支持私聊表情包/动图自动转文件，静态自定义表情精准识别，动态 WebP 自动转 GIF")
class StickerConvert(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        # 1. 初始化存储目录
        self.data_dir = os.path.join(os.getcwd(), "data", "sticker_convert")
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 2. 初始化 SQLite 数据库
        self.db_path = os.path.join(self.data_dir, "stickers.db")
        self._init_db()

        # 3. 内存缓存最近收到的图片/表情（按发送者隔离），用于无引用指令时的智能回溯与转发连续转换
        # {sender_id: [{"url": str, "time": float, "used": bool}, ...]}
        self.recent_images: dict[str, list[dict]] = {}
        # 发送锁，避免高频并发发送文件卡片导致冲突
        self._send_lock = asyncio.Lock()

    def _init_db(self):
        """建表"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS sticker_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT,
                    md5 TEXT,
                    ext TEXT,
                    mime TEXT,
                    size INTEGER,
                    is_gif BOOLEAN,
                    file_name TEXT,
                    file_path TEXT,
                    uploader_id TEXT,
                    created_at TIMESTAMP
                )
            ''')

    def _convert_animated_webp_to_gif(self, buffer: bytes):
        """将多帧 WebP 转换为 GIF；静态 WebP 或缺少 Pillow 时返回 None。"""
        if PILImage is None:
            return None

        try:
            with PILImage.open(BytesIO(buffer)) as source:
                if not getattr(source, "is_animated", False) or getattr(source, "n_frames", 1) <= 1:
                    return None

                frames = []
                durations = []
                frame_count = source.n_frames
                loop = source.info.get("loop", 0)

                for frame_index in range(frame_count):
                    source.seek(frame_index)
                    frames.append(source.convert("RGBA").copy())
                    duration = source.info.get("duration", 100)
                    durations.append(max(20, int(duration or 100)))

                output = BytesIO()
                frames[0].save(
                    output,
                    format="GIF",
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=loop,
                    disposal=2,
                    optimize=False,
                )
                return output.getvalue()
        except Exception as exc:
            logger.warning(f"[StickerConvert] 动态 WebP 转 GIF 失败，保留原文件: {exc}")
            return None

    async def _download_image(self, url: str):
        """异步下载图片并返回 buffer, mime, ext"""
        if os.path.isfile(url):
            buffer = await asyncio.to_thread(Path(url).read_bytes)
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=30) as response:
                    if response.status != 200:
                        raise Exception(f"HTTP Error {response.status}")
                    buffer = await response.read()
        mime, ext = 'image/unknown', 'jpg'
        if buffer.startswith(b'\x89PNG'):
            mime, ext = 'image/png', 'png'
        elif buffer.startswith(b'\xff\xd8\xff'):
            mime, ext = 'image/jpeg', 'jpg'
        elif buffer.startswith(b'GIF8'):
            mime, ext = 'image/gif', 'gif'
        elif buffer[0:4] == b'RIFF' and buffer[8:12] == b'WEBP':
            mime, ext = 'image/webp', 'webp'
            if self.config.get("convert_animated_webp_to_gif", True):
                converted = self._convert_animated_webp_to_gif(buffer)
                if converted is not None:
                    buffer, mime, ext = converted, 'image/gif', 'gif'
        return buffer, mime, ext

    def _is_animated_webp(self, buffer: bytes) -> bool:
        """判断 WebP 图片是否为动图（多帧）"""
        if not buffer or len(buffer) < 21:
            return False
        # 1. 优先使用 Pillow 检测
        if PILImage is not None:
            try:
                with PILImage.open(BytesIO(buffer)) as img:
                    return bool(getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1)
            except Exception:
                pass
        # 2. 备选方案：检查 WebP 规范中的 Extended header VP8X 动画标识位
        try:
            if buffer[0:4] == b"RIFF" and buffer[8:12] == b"WEBP" and buffer[12:16] == b"VP8X":
                return bool(buffer[20] & 0x02)
        except Exception:
            pass
        return False

    def _is_animated_image(self, buffer: bytes, mime: str = "", ext: str = "") -> bool:
        """判断图片是否为动图（GIF、动态 WebP、APNG 等）"""
        if not buffer:
            return False
        # 1. GIF 格式：在 QQ 手机端与桌面端，任何 GIF 均作为表情动图展示且无法直接保存至相册
        if buffer.startswith(b"GIF8") or mime == "image/gif" or ext == "gif":
            return True
        # 2. 动态 WebP
        if buffer[0:4] == b"RIFF" and buffer[8:12] == b"WEBP":
            return self._is_animated_webp(buffer)
        # 3. APNG 或通用 Pillow 动图检查
        if PILImage is not None:
            try:
                with PILImage.open(BytesIO(buffer)) as img:
                    if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                        return True
            except Exception:
                pass
        return False

    def _is_valid_sticker(self, buffer: bytes, mime: str, ext: str, is_sticker_meta: bool) -> tuple[bool, str]:
        """综合判定该图片是否属于“无法直接保存至相册的表情包”
        返回: (is_sticker: bool, reason: str)
        1. 动图 (GIF、动态 WebP、APNG): 在 QQ 中一律作为动图表情呈现，无法直接保存至相册 -> 认定为表情包
        2. 带表情包元数据 (mface、sub_type=1、带[动画表情]摘要等) -> 认定为表情包
        3. 静态图片尺寸与体积判定 (覆盖用户收藏的静态表情、静态咖波、熊猫头梗图等):
           QQ 中用户收藏的表情包尺寸普遍较小 (通常 <= 800px，绝大部分在 200~500px，且体积 <= 2MB)。
           而相机拍摄的照片、手机截屏等通常为大图 (1080x1920, 2400x1080, 3000x4000 等)。
           当静态图片长边 <= max_sticker_dimension (默认 800) 且体积 <= 2MB 时，认定为静态表情包；
           超过阈值则判定为普通照片/手机截屏，予以跳过。
        """
        # 1. 动图判断
        if self._is_animated_image(buffer, mime, ext):
            return True, "动图表情(GIF/动态WebP/APNG)"

        # 2. 表情包元数据判断
        if is_sticker_meta:
            return True, "包含表情包元数据(mface/sub_type/表情摘要)"

        # 3. 静态图片尺寸与体积启发式检测
        max_dim_threshold = int(self.config.get("max_sticker_dimension", 800))
        max_size_bytes = int(self.config.get("max_sticker_bytes", 2 * 1024 * 1024))

        if len(buffer) > max_size_bytes:
            return False, f"静态图片文件体积过大 ({round(len(buffer)/1024/1024, 2)}MB > {round(max_size_bytes/1024/1024, 2)}MB)"

        if PILImage is not None:
            try:
                with PILImage.open(BytesIO(buffer)) as img:
                    w, h = img.size
                    max_dim = max(w, h)
                    if max_dim <= max_dim_threshold:
                        return True, f"静态表情包 (尺寸 {w}x{h} <= {max_dim_threshold}px)"
                    else:
                        return False, f"普通大图/截屏 (尺寸 {w}x{h} > {max_dim_threshold}px)"
            except Exception as exc:
                logger.warning(f"[StickerConvert] Pillow 解析静态图片尺寸失败: {exc}")

        return False, "静态图片且无表情元数据"

    def _extract_image_items_from_current_msg(self, event: AstrMessageEvent) -> list[dict]:
        """从单条消息组件链及 raw_message 中提取图片/表情信息，精确区分普通图片与表情包元数据。
        排除 File 文件消息段，识别 mface、sub_type=1 以及带表情摘要的表情包。
        返回: [{"url": str, "is_sticker_meta": bool}, ...]
        """
        items_map = {}  # url -> is_sticker_meta

        def _add_item(u, is_sticker_meta=False):
            if u and isinstance(u, str):
                u = u.strip()
                if u.startswith("http://") or u.startswith("https://"):
                    u_lower = u.lower()
                    if any(k in u_lower for k in ("subtype=1", "term=2", "custom_face", "offpic")):
                        is_sticker_meta = True
                    if u not in items_map:
                        items_map[u] = is_sticker_meta
                    else:
                        items_map[u] = items_map[u] or is_sticker_meta

        # 1. 优先从 OneBot 原始数据 raw_message 中提取（包含 sub_type、summary、mface 等丰富元数据）
        raw_event = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_event, dict):
            raw_msg = raw_event.get("message")
            if isinstance(raw_msg, list):
                for seg in raw_msg:
                    if not isinstance(seg, dict):
                        continue
                    seg_type = str(seg.get("type", "")).lower()
                    # 明确排除纯文件消息段，绝不作为表情提取转发
                    if seg_type == "file":
                        continue

                    data = seg.get("data", {})
                    if not isinstance(data, dict):
                        continue

                    if seg_type in ("image", "mface", "marketface", "bface", "sface"):
                        is_mface = seg_type in ("mface", "marketface", "bface", "sface")
                        sub_type = str(data.get("sub_type") or data.get("subType") or data.get("subtype") or "").strip()
                        is_sub_type_sticker = sub_type in ("1", "7", "8", "9")
                        summary = str(data.get("summary") or data.get("name") or "").strip()
                        is_summary_sticker = any(kw in summary for kw in ("表情", "动画", "热图"))

                        is_sticker_meta = is_mface or is_sub_type_sticker or is_summary_sticker

                        for key in ("url", "cdnurl", "cdn_url", "raw_url", "origin_url", "original_url", "thumb", "thumb_url", "file"):
                            _add_item(data.get(key), is_sticker_meta)
            elif isinstance(raw_msg, str):
                for match in re.finditer(r'\[CQ:(image|mface|marketface|bface|sface)(?:,([^\]]+))?\]', raw_msg, re.I):
                    seg_type = match.group(1).lower()
                    params_str = match.group(2) or ''
                    params = dict(re.findall(r'([a-zA-Z0-9_-]+)=([^,\]]+)', params_str))
                    url = params.get('url') or params.get('cdnurl') or params.get('file')
                    sub_type = str(params.get('subType') or params.get('sub_type') or params.get('subtype') or '').strip()
                    summary = str(params.get('summary', '')).strip()
                    is_sticker_meta = (
                        seg_type in ('mface', 'marketface', 'bface', 'sface')
                        or sub_type in ('1', '7', '8', '9')
                        or any(kw in summary for kw in ('表情', '动画', '热图'))
                    )
                    _add_item(url, is_sticker_meta)

        # 2. 从 AstrBot 消息组件中提取 Image（排除 File 与 Reply 组件）
        for comp in getattr(event.message_obj, "message", []) or []:
            comp_type_str = str(getattr(comp, "type", "")).lower()
            cls_name = getattr(comp, "__class__", None).__name__ if getattr(comp, "__class__", None) else ""
            if isinstance(comp, Reply) or cls_name == "Reply" or comp_type_str.endswith("reply"):
                continue
            if cls_name == "File" or comp_type_str.endswith("file"):
                continue

            is_img = (
                isinstance(comp, Image)
                or cls_name == "Image"
                or comp_type_str.endswith("image")
                or bool(getattr(comp, "url", None))
            )
            if is_img:
                url_attr = getattr(comp, "url", None)
                if url_attr and isinstance(url_attr, str):
                    _add_item(url_attr, False)
                file_attr = getattr(comp, "file", None)
                if file_attr and isinstance(file_attr, str):
                    _add_item(file_attr, False)

        return [{"url": u, "is_sticker_meta": meta} for u, meta in items_map.items()]

    def _extract_urls_from_current_msg(self, event: AstrMessageEvent) -> list[str]:
        """从单条消息组件链及 raw_message 中提取全部图片/表情 URL（兼容上层调用）"""
        return [item["url"] for item in self._extract_image_items_from_current_msg(event)]

    async def _extract_image_urls(self, event: AstrMessageEvent, allow_recent: bool = True):
        """从消息、引用的历史消息或近期私聊上下文中提取图片 URL"""
        if event.get_platform_name() == "telegram":
            components = list(event.get_messages())
            for component in event.get_messages():
                if isinstance(component, Reply):
                    components.extend(component.chain or [])
            return [
                await component.convert_to_file_path()
                for component in components if isinstance(component, Image)
            ]
        sender_id = str(event.message_obj.sender.user_id) if hasattr(event.message_obj, "sender") and hasattr(event.message_obj.sender, "user_id") else ""

        # 1. 优先提取当前消息自带的图片
        urls = self._extract_urls_from_current_msg(event)
        if urls:
            return urls

        # 2. 检查是否有引用回复 (Reply)
        for comp in getattr(event.message_obj, "message", []) or []:
            is_reply = (
                isinstance(comp, Reply)
                or comp.__class__.__name__ == "Reply"
                or "reply" in str(getattr(comp, "type", "")).lower()
                or hasattr(comp, "chain")
                or hasattr(comp, "id")
            )
            if is_reply:
                # 2.1 优先从 AstrBot 原生解析的 comp.chain 中提取
                chain = getattr(comp, "chain", None)
                if isinstance(chain, list):
                    for item in chain:
                        is_item_img = (
                            isinstance(item, Image)
                            or item.__class__.__name__ == "Image"
                            or "image" in str(getattr(item, "type", "")).lower()
                            or hasattr(item, "url")
                            or hasattr(item, "file")
                        )
                        if is_item_img:
                            u = getattr(item, "url", None) or getattr(item, "file", None)
                            if u and isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
                                urls.append(u)
                if urls:
                    return urls

                # 2.2 若 comp.chain 未获取到，调用 OneBot 的 get_msg 接口二次获取
                msg_id = getattr(comp, "id", None)
                try:
                    if event.get_platform_name() == "aiocqhttp" and msg_id is not None:
                        msg_id_param = int(msg_id) if str(msg_id).lstrip("-").isdigit() else msg_id
                        res = await event.bot.api.call_action('get_msg', message_id=msg_id_param)
                        if isinstance(res, dict) and "data" in res and isinstance(res["data"], dict) and "message" in res["data"]:
                            msg_data = res["data"]["message"]
                        else:
                            msg_data = res.get('message', []) if isinstance(res, dict) else []

                        if isinstance(msg_data, list):
                            for m in msg_data:
                                if not isinstance(m, dict):
                                    continue
                                m_type = str(m.get('type', '')).lower()
                                data = m.get('data', {})
                                if isinstance(data, dict):
                                    if m_type in ('image', 'mface', 'marketface', 'bface'):
                                        for k in ('url', 'cdnurl', 'cdn_url', 'raw_url', 'file'):
                                            v = data.get(k)
                                            if v and isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                                                urls.append(v)
                        elif isinstance(msg_data, str):
                            matches = re.findall(r'\[CQ:(?:image|mface|marketface).*?url=([^,\]]+)', msg_data, re.I)
                            urls.extend(matches)
                except Exception as e:
                    logger.warning(f"[StickerConvert] 引用详情获取失败: {e}")

                if urls:
                    return urls

                # 2.3 若 get_msg 失败或未找到（常见于转发消息没有被 OneBot 缓存），从本地近期缓存中按 msg_id 匹配
                if sender_id and msg_id is not None:
                    user_cache = self.recent_images.get(sender_id, [])
                    matched = [item for item in user_cache if item.get("msg_id") and str(item["msg_id"]) == str(msg_id)]
                    if matched:
                        for item in matched:
                            urls.append(item["url"])
                            item["used"] = True
                        return urls

        # 3. 逐条转发与未引用连续转换支持：若当前无图片无引用，且允许回溯（如私聊中连续转发多张后发送 /表情转换）
        if allow_recent and sender_id:
            user_cache = self.recent_images.get(sender_id, [])
            now = time.time()
            # 查找 120 秒内未消费的近期表情
            unconsumed = [item for item in user_cache if not item["used"] and now - item["time"] <= 120]
            if unconsumed:
                for item in unconsumed:
                    item["used"] = True
                    urls.append(item["url"])
                return urls

            # 若均已标记消费，则回溯 60 秒内最后收到的 1 张
            recent_valid = [item for item in user_cache if now - item["time"] <= 60]
            if recent_valid:
                urls.append(recent_valid[-1]["url"])
                return urls

        return urls

    async def _send_as_file(self, event: AstrMessageEvent, buffer: bytes, file_name: str):
        """绕过框架和硬盘隔离，通过 Base64 直接向底层发送纯文件卡片"""
        if event.get_platform_name() == "telegram":
            path = Path(self.data_dir) / "tg_out" / Path(file_name).name
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, buffer)
            try:
                await event.send(MessageChain([File(name=path.name, file=str(path))]))
                return True
            finally:
                path.unlink(missing_ok=True)
        if event.get_platform_name() == "aiocqhttp":
            b64_data = base64.b64encode(buffer).decode()
            b64_uri = f"base64://{b64_data}"
            
            group_id = getattr(event.message_obj, 'group_id', None)
            try:
                if group_id:
                    await event.bot.api.call_action('upload_group_file', group_id=int(group_id), file=b64_uri, name=file_name)
                else:
                    user_id = event.message_obj.sender.user_id
                    await event.bot.api.call_action('upload_private_file', user_id=int(user_id), file=b64_uri, name=file_name)
                return True
            except Exception as e:
                logger.warning(f"[StickerConvert] API 文件发送失败: {e}")
                return False
        return False

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊消息监听：记录近期表情，以及可选的自动将表情包/动图转换为文件发出"""
        sender_id = str(event.message_obj.sender.user_id)
        msg_id = getattr(event.message_obj, "message_id", None)
        items = self._extract_image_items_from_current_msg(event)

        # 1. 记录到用户近期表情缓存
        if items:
            now = time.time()
            if sender_id not in self.recent_images:
                self.recent_images[sender_id] = []
            for item in items:
                self.recent_images[sender_id].append({
                    "url": item["url"],
                    "is_sticker_meta": item.get("is_sticker_meta", False),
                    "time": now,
                    "msg_id": str(msg_id) if msg_id is not None else None,
                    "used": False
                })
            # 保留最近 30 条，清理超过 300 秒的过期记录
            self.recent_images[sender_id] = [
                r for r in self.recent_images[sender_id] if now - r["time"] <= 300
            ][-30:]

        # 2. 如果开启了私聊收到表情包自动转换为文件功能
        if self.config.get("auto_convert_private", False) and items:
            # 若消息带有其他指令前缀，不进行自动转换，交给对应指令处理
            msg_str = (event.get_message_str() or "").strip()
            if msg_str.startswith(("/", "／", "!", "！")):
                return

            logger.info(f"[StickerConvert] 收到私聊消息 (sender={sender_id}, msg_id={msg_id})，分析 {len(items)} 个候选图片/表情")
            converted_urls = set()
            # 逐张检测并转换为文件卡片
            for item in items:
                url = item["url"]
                is_sticker_meta = item.get("is_sticker_meta", False)

                try:
                    buffer, mime, ext = await self._download_image(url)

                    # 综合判断是否属于“无法直接保存至相册的表情包”（动图/表情包元数据/小尺寸静态表情包）
                    is_sticker, reason = self._is_valid_sticker(buffer, mime, ext, is_sticker_meta)
                    if not is_sticker:
                        logger.info(f"[StickerConvert] 跳过非表情包图片: {reason} (url={url[:60]}...)")
                        continue

                    md5 = hashlib.md5(buffer).hexdigest()
                    file_name = f"emoji_{md5[:8]}.{ext}"
                    logger.info(f"[StickerConvert] 识别为表情包 ({reason})，正在发送文件卡片: {file_name}")

                    async with self._send_lock:
                        success = await self._send_as_file(event, buffer, file_name)
                        if success:
                            converted_urls.add(url)
                            logger.info(f"[StickerConvert] 自动转文件发送成功: {file_name}")
                        else:
                            logger.warning(f"[StickerConvert] 自动转文件发送失败: {file_name}")
                except Exception as e:
                    logger.error(f"[StickerConvert] 私聊自动转换异常: {e}", exc_info=True)

            # 仅标记成功发送的表情为已使用
            if sender_id in self.recent_images and converted_urls:
                for r_item in self.recent_images[sender_id]:
                    if r_item["url"] in converted_urls:
                        r_item["used"] = True

            # 仅在真正发送了转换文件时，才阻断事件传递
            if converted_urls:
                event.stop_event()

    @filter.command("表情转换")
    async def convert_only(self, event: AstrMessageEvent):
        """仅转换，不保存"""
        allow_recent = event.is_private_chat()
        urls = await self._extract_image_urls(event, allow_recent=allow_recent)
        if not urls:
            yield event.plain_result("⚠️ 请引用包含表情的消息，或者在发图的同时配上文字 /表情转换。")
            return

        yield event.plain_result("⏳ 正在为您提取并打包为文件，请稍候...")
        
        for url in urls:
            try:
                buffer, mime, ext = await self._download_image(url)
                md5 = hashlib.md5(buffer).hexdigest()
                file_name = f"emoji_{md5[:8]}.{ext}"
                
                async with self._send_lock:
                    success = await self._send_as_file(event, buffer, file_name)
                if not success:
                    yield event.plain_result("❌ 文件发送失败，可能平台不受支持。")
            except Exception as e:
                yield event.plain_result(f"❌ 转换失败: {str(e)}")

    @filter.command("表情转存")
    async def convert_and_save(self, event: AstrMessageEvent):
        """转存到本地相册（按用户个人隔离）"""
        allow_recent = event.is_private_chat()
        urls = await self._extract_image_urls(event, allow_recent=allow_recent)
        if not urls:
            yield event.plain_result("⚠️ 请引用包含表情的消息。")
            return
            
        # 强制使用用户的 QQ 号作为唯一身份标识，实现个人相册隔离
        uploader_id = str(event.message_obj.sender.user_id)
        
        for url in urls:
            try:
                buffer, mime, ext = await self._download_image(url)
                md5 = hashlib.md5(buffer).hexdigest()
                
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    # 查重范围限定在个人的相册中
                    cursor.execute("SELECT id FROM sticker_archive WHERE uploader_id=? AND md5=?", (uploader_id, md5))
                    if cursor.fetchone():
                        yield event.plain_result("📁 此表情已存在您的个人相册中！")
                        continue

                file_name = f"{datetime.now().strftime('%Y-%m-%d')}-{md5}.{ext}"
                file_path = os.path.join(self.data_dir, file_name)
                
                with open(file_path, 'wb') as f:
                    f.write(buffer)
                    
                with sqlite3.connect(self.db_path) as conn:
                    # 抛弃 channel_id 的使用，统统存为 uploader_id
                    conn.execute('''
                        INSERT INTO sticker_archive 
                        (channel_id, md5, ext, mime, size, is_gif, file_name, file_path, uploader_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (uploader_id, md5, ext, mime, len(buffer), ext == 'gif', file_name, file_path, uploader_id, datetime.now()))
                
                yield event.plain_result("✅ 转存成功！为您发送实体文件：")
                async with self._send_lock:
                    await self._send_as_file(event, buffer, file_name)
                    
            except Exception as e:
                yield event.plain_result(f"❌ 转存失败: {str(e)}")

    @filter.command("表情相册")
    async def view_album(self, event: AstrMessageEvent, page: int = 1):
        """查看个人的表情相册"""
        uploader_id = str(event.message_obj.sender.user_id)
        page_size = 5
        offset = (page - 1) * page_size
        
        if page <= 0:
            yield event.plain_result("❌ 页码必须大于 0。")
            return

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM sticker_archive WHERE uploader_id=?", (uploader_id,))
            total = cursor.fetchone()[0]
            
            if total == 0:
                yield event.plain_result("📭 您的个人相册为空，快去转存一些表情吧！")
                return
                
            # 按 ID 升序排列，这样展示的序号就是稳定的 1, 2, 3...
            cursor.execute('''
                SELECT file_name, size, is_gif 
                FROM sticker_archive 
                WHERE uploader_id=? 
                ORDER BY id ASC 
                LIMIT ? OFFSET ?
            ''', (uploader_id, page_size, offset))
            records = cursor.fetchall()

        total_pages = (total + page_size - 1) // page_size
        if not records:
            yield event.plain_result("没有更多表情了。")
            return

        msg_chain = [Plain(f"📱 您的表情相册 (第 {page}/{total_pages} 页，共 {total} 个)\n\n")]
        
        for idx, record in enumerate(records):
            name, size, is_gif = record
            icon = "🎞️" if is_gif else "🖼️"
            kb_size = round(size / 1024, 1)
            
            # 动态计算连续序号：前面跳过的数量(offset) + 当前页的索引(idx) + 1
            display_id = offset + idx + 1
            
            msg_chain.append(Plain(f"{display_id}. {icon} {name} ({kb_size}KB)\n"))
            
            file_path = os.path.join(self.data_dir, name)
            if os.path.exists(file_path):
                msg_chain.append(Image.fromFileSystem(file_path))
                msg_chain.append(Plain("\n\n"))
            
        msg_chain.append(Plain("💡 使用 /表情相册发送 <编号> 来提取表情\n💡 使用 /表情相册删除 <编号> 来删除表情"))
        yield event.chain_result(msg_chain)

    @filter.command("表情相册发送")
    async def send_album_emoji(self, event: AstrMessageEvent, emoji_id: int):
        """发送个人相册中指定序号的表情"""
        uploader_id = str(event.message_obj.sender.user_id)
        
        if emoji_id <= 0:
            yield event.plain_result("❌ 序号必须是大于 0 的正整数。")
            return
            
        # 根据用户输入的连续序号，转换为数据库查询时的偏移量 (序号 - 1)
        offset = emoji_id - 1
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT file_path, file_name 
                FROM sticker_archive 
                WHERE uploader_id=? 
                ORDER BY id ASC 
                LIMIT 1 OFFSET ?
            ''', (uploader_id, offset))
            record = cursor.fetchone()
            
        if not record:
            yield event.plain_result(f"❌ 找不到序号为 {emoji_id} 的表情。")
            return
            
        file_path, file_name = record
        if not os.path.exists(file_path):
            yield event.plain_result("❌ 本地文件已丢失！")
            return
            
        with open(file_path, 'rb') as f:
            buffer = f.read()
            
        async with self._send_lock:
            await self._send_as_file(event, buffer, file_name)

    @filter.command("表情相册删除")
    async def delete_album_emoji(self, event: AstrMessageEvent, emoji_id: int):
        """删除个人相册中指定序号的表情"""
        uploader_id = str(event.message_obj.sender.user_id)
        
        if emoji_id <= 0:
            yield event.plain_result("❌ 序号必须是大于 0 的正整数。")
            return
            
        offset = emoji_id - 1
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 拿到真实数据行主键ID用于删除
            cursor.execute('''
                SELECT id, file_path 
                FROM sticker_archive 
                WHERE uploader_id=? 
                ORDER BY id ASC 
                LIMIT 1 OFFSET ?
            ''', (uploader_id, offset))
            record = cursor.fetchone()
            
            if not record:
                yield event.plain_result(f"❌ 找不到序号为 {emoji_id} 的表情。")
                return
                
            db_id, file_path = record
            if os.path.exists(file_path):
                os.remove(file_path)
                
            cursor.execute("DELETE FROM sticker_archive WHERE id=?", (db_id,))
            conn.commit()
            
        yield event.plain_result(f"✅ 已成功删除表情序号: {emoji_id}。您的相册序号已自动重新排列。")
