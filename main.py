import os
import sqlite3
import hashlib
import aiohttp
import asyncio
import re
import base64
from io import BytesIO
from datetime import datetime
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Image, Plain, Reply

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

@register("sticker_convert", "Author", "QQ表情转存与文件提取插件", "1.0.7", "将QQ表情转存并以纯文件格式发送，动态 WebP 自动转 GIF")
class StickerConvert(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 1. 初始化存储目录
        self.data_dir = os.path.join(os.getcwd(), "data", "sticker_convert")
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 2. 初始化 SQLite 数据库
        self.db_path = os.path.join(self.data_dir, "stickers.db")
        self._init_db()

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
            print(f"[StickerConvert] 动态 WebP 转 GIF 失败，保留原文件: {exc}")
            return None

    async def _download_image(self, url: str):
        """异步下载图片并返回 buffer"""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    raise Exception(f"HTTP Error {response.status}")
                buffer = await response.read()
                
                mime = 'image/unknown'
                ext = 'jpg'
                if buffer.startswith(b'\x89PNG'):
                    mime, ext = 'image/png', 'png'
                elif buffer.startswith(b'\xff\xd8\xff'):
                    mime, ext = 'image/jpeg', 'jpg'
                elif buffer.startswith(b'GIF8'):
                    mime, ext = 'image/gif', 'gif'
                elif buffer[0:4] == b'RIFF' and buffer[8:12] == b'WEBP':
                    mime, ext = 'image/webp', 'webp'

                    converted = self._convert_animated_webp_to_gif(buffer)
                    if converted is not None:
                        buffer = converted
                        mime, ext = 'image/gif', 'gif'
                    
                return buffer, mime, ext

    async def _extract_image_urls(self, event: AstrMessageEvent):
        """从消息或引用的历史消息中提取图片 URL"""
        urls = []
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                urls.append(comp.url)
                
        if not urls:
            for comp in event.message_obj.message:
                if isinstance(comp, Reply):
                    try:
                        if event.get_platform_name() == "aiocqhttp":
                            res = await event.bot.api.call_action('get_msg', message_id=comp.id)
                            msg_data = res.get('message', [])
                            if isinstance(msg_data, list):
                                for m in msg_data:
                                    if m.get('type') == 'image' and 'url' in m.get('data', {}):
                                        urls.append(m['data']['url'])
                            elif isinstance(msg_data, str):
                                matches = re.findall(r'\[CQ:image,.*?url=([^,\]]+)', msg_data)
                                urls.extend(matches)
                    except Exception:
                        pass 
        return urls

    async def _send_as_file(self, event: AstrMessageEvent, buffer: bytes, file_name: str):
        """绕过框架和硬盘隔离，通过 Base64 直接向底层发送纯文件卡片"""
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
                print(f"[StickerConvert] API 文件发送失败: {e}")
                return False
        return False

    @filter.command("表情转换")
    async def convert_only(self, event: AstrMessageEvent):
        """仅转换，不保存"""
        urls = await self._extract_image_urls(event)
        if not urls:
            yield event.plain_result("⚠️ 请引用包含表情的消息，或者在发图的同时配上文字 /表情转换。")
            return

        yield event.plain_result("⏳ 正在为您提取并打包为文件，请稍候...")
        
        for url in urls:
            try:
                buffer, mime, ext = await self._download_image(url)
                md5 = hashlib.md5(buffer).hexdigest()
                file_name = f"emoji_{md5[:8]}.{ext}"
                
                success = await self._send_as_file(event, buffer, file_name)
                if not success:
                    yield event.plain_result("❌ 文件发送失败，可能平台不受支持。")
            except Exception as e:
                yield event.plain_result(f"❌ 转换失败: {str(e)}")

    @filter.command("表情转存")
    async def convert_and_save(self, event: AstrMessageEvent):
        """转存到本地相册（按用户个人隔离）"""
        urls = await self._extract_image_urls(event)
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
