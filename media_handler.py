"""
媒体处理模块 - 处理用户发送的图片、语音、视频、文件
"""
import os
import time
import logging
import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

BJ_TZ = ZoneInfo('Asia/Shanghai')

# 媒体存储根目录
MEDIA_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'media')

# 目录结构
MEDIA_DIRS = {
    'images': os.path.join(MEDIA_ROOT, 'images'),
    'voice': os.path.join(MEDIA_ROOT, 'voice'),
    'video': os.path.join(MEDIA_ROOT, 'video'),
    'files': os.path.join(MEDIA_ROOT, 'files'),
}

# 文件大小限制 (字节)
MAX_SIZES = {
    'image': 10 * 1024 * 1024,    # 10MB
    'voice': 5 * 1024 * 1024,     # 5MB
    'video': 50 * 1024 * 1024,    # 50MB
    'file': 20 * 1024 * 1024,     # 20MB
}


def _ensure_dirs():
    """确保媒体目录存在"""
    for dir_path in MEDIA_DIRS.values():
        os.makedirs(dir_path, exist_ok=True)


def _generate_filename(user_id: str, original_name: str, media_type: str) -> str:
    """生成唯一文件名: {user_id}_{timestamp}_{hash}_{original_name}"""
    timestamp = datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')
    hash_str = hashlib.md5(f"{user_id}_{time.time()}".encode()).hexdigest()[:8]
    ext = os.path.splitext(original_name)[1] if original_name else ''
    if not ext:
        ext_map = {
            'images': '.png',
            'voice': '.amr',
            'video': '.mp4',
            'files': '.dat',
        }
        ext = ext_map.get(media_type, '.dat')
    return f"{user_id}_{timestamp}_{hash_str}{ext}"


def _check_file_size(data: bytes, media_type: str) -> bool:
    """检查文件大小是否超限"""
    max_size = MAX_SIZES.get(media_type, MAX_SIZES['file'])
    if len(data) > max_size:
        logger.warning("File too large: %d bytes (max %d)", len(data), max_size)
        return False
    return True


def _download_and_save(url: str, user_id: str, media_type: str, original_name: str) -> str:
    """下载并保存媒体文件，返回本地路径"""
    import requests

    _ensure_dirs()

    try:
        resp = requests.get(url, timeout=120, stream=True)
        if resp.status_code != 200:
            logger.error("Download failed: HTTP %d", resp.status_code)
            return None

        # 流式读取并检查大小
        data = b''
        for chunk in resp.iter_content(chunk_size=8192):
            data += chunk
            if not _check_file_size(data, media_type):
                return None

        filename = _generate_filename(user_id, original_name, media_type)
        save_path = os.path.join(MEDIA_DIRS[media_type + 's'], filename)

        with open(save_path, 'wb') as f:
            f.write(data)

        logger.info("Saved %s to %s (%d bytes)", media_type, save_path, len(data))
        return save_path
    except Exception as e:
        logger.error("Download and save error: %s", e)
        return None


def process_image(url: str, user_id: str) -> dict:
    """处理用户发送的图片
    返回: {'path': 本地路径, 'analysis': AI分析结果}
    """
    save_path = _download_and_save(url, user_id, 'image', 'image.png')
    if not save_path:
        return None

    result = {'path': save_path, 'analysis': None}

    # AI 分析图片内容
    try:
        analysis = _analyze_image(save_path)
        result['analysis'] = analysis
    except Exception as e:
        logger.error("Image analysis error: %s", e)
        result['analysis'] = "图片已保存，但分析失败"

    return result


def _analyze_image(image_path: str) -> str:
    """使用 AI 分析图片内容"""
    try:
        import base64
        from openai import OpenAI
        import config_secrets

        client = OpenAI(
            api_key=config_secrets.MIMO_API_KEY,
            base_url=config_secrets.MIMO_API_BASE,
        )

        with open(image_path, 'rb') as f:
            image_data = base64.b64encode(f.read()).decode()

        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.bmp': 'image/bmp',
            '.webp': 'image/webp',
        }
        mime_type = mime_map.get(ext, 'image/png')

        response = client.chat.completions.create(
            model=config_secrets.MIMO_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "请描述这张图片的内容，重点关注：1) 图片中是否有管网相关的设备、图表、数据；2) 是否有异常情况；3) 图片的主要内容是什么。请用简洁的中文回答。"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}},
                ],
            }],
            max_tokens=500,
        )

        return response.choices[0].message.content
    except Exception as e:
        logger.error("AI image analysis error: %s", e)
        return None


def process_voice(url: str, user_id: str, duration: int = 0) -> str:
    """处理用户发送的语音，返回转文字结果"""
    save_path = _download_and_save(url, user_id, 'voice', 'voice.amr')
    if not save_path:
        return None

    # 尝试语音转文字
    text = _speech_to_text(save_path)
    if text:
        return text

    return f"语音已保存（时长 {duration} 秒），但转文字失败"


def _speech_to_text(audio_path: str) -> str:
    """语音转文字（使用 MiMo 或其他 STT API）"""
    try:
        from openai import OpenAI
        import config_secrets

        client = OpenAI(
            api_key=config_secrets.MIMO_API_KEY,
            base_url=config_secrets.MIMO_API_BASE,
        )

        with open(audio_path, 'rb') as f:
            response = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="zh",
            )

        return response.text
    except Exception as e:
        logger.error("Speech to text error: %s", e)
        return None


def process_video(url: str, user_id: str, duration: int = 0) -> dict:
    """处理用户发送的视频
    返回: {'path': 本地路径, 'duration': 时长}
    """
    save_path = _download_and_save(url, user_id, 'video', 'video.mp4')
    if not save_path:
        return None

    return {'path': save_path, 'duration': duration}


def process_file(url: str, user_id: str, file_name: str, file_size: int = 0) -> dict:
    """处理用户发送的文件
    返回: {'path': 本地路径, 'content': 文件内容摘要}
    """
    save_path = _download_and_save(url, user_id, 'files', file_name)
    if not save_path:
        return None

    result = {'path': save_path, 'content': None}

    # 尝试解析文件内容
    ext = os.path.splitext(file_name)[1].lower()
    content = _parse_file(save_path, ext)
    if content:
        result['content'] = content

    return result


def _parse_file(file_path: str, ext: str) -> str:
    """解析文件内容"""
    try:
        if ext == '.txt' or ext == '.csv':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read(2000)  # 读取前 2000 字符
            return f"文件内容预览：\n{content}"

        elif ext in ('.pdf',):
            try:
                import PyPDF2
                with open(file_path, 'rb') as f:
                    reader = PyPDF2.PdfReader(f)
                    text = ''
                    for i, page in enumerate(reader.pages[:3]):  # 读取前 3 页
                        text += page.extract_text() or ''
                    if text:
                        return f"PDF 内容预览（前3页）：\n{text[:2000]}"
            except ImportError:
                pass

        elif ext in ('.xls', '.xlsx'):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True)
                ws = wb.active
                rows = []
                for i, row in enumerate(ws.iter_rows(max_row=10, values_only=True)):
                    rows.append(str(row))
                wb.close()
                if rows:
                    return f"Excel 内容预览（前10行）：\n" + "\n".join(rows)
            except ImportError:
                pass

        elif ext in ('.doc', '.docx'):
            return "Word 文档已保存，需要安装 python-docx 库才能解析内容"

        return None
    except Exception as e:
        logger.error("Parse file error: %s", e)
        return None


def cleanup_old_media(days: int = 30):
    """清理超过指定天数的媒体文件"""
    _ensure_dirs()
    cutoff_time = time.time() - (days * 86400)
    deleted = 0

    for dir_path in MEDIA_DIRS.values():
        if not os.path.exists(dir_path):
            continue
        for filename in os.listdir(dir_path):
            filepath = os.path.join(dir_path, filename)
            if os.path.isfile(filepath):
                if os.path.getmtime(filepath) < cutoff_time:
                    try:
                        os.remove(filepath)
                        deleted += 1
                    except Exception as e:
                        logger.error("Delete file error: %s", e)

    logger.info("Cleaned up %d old media files", deleted)
    return deleted
