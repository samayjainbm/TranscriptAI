from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import unescape
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from yt_dlp import YoutubeDL


ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / '.transcript_cache'
CACHE_DIR.mkdir(exist_ok=True)


def extract_video_id(video_url: str) -> str | None:
    parsed = urlparse(video_url)
    if 'youtu.be' in parsed.netloc:
        return parsed.path.lstrip('/') or None
    if 'youtube.com' in parsed.netloc:
        params = parse_qs(parsed.query)
        value = (params.get('v') or [None])[0]
        return value
    match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', video_url)
    return match.group(1) if match else None


def cache_path(video_id: str, lang: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', lang)
    return CACHE_DIR / f'{video_id}__{safe}.json'


def read_cache(video_id: str, lang: str) -> dict | None:
    path = cache_path(video_id, lang)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def read_any_cache(video_id: str) -> dict | None:
    for path in CACHE_DIR.glob(f'{video_id}__*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('segments'):
                return data
        except Exception:
            continue
    return None


def write_cache(video_id: str, lang: str, payload: dict) -> None:
    try:
        cache_path(video_id, lang).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def is_rate_limited_error(exc: Exception) -> bool:
    message = str(exc)
    return '429' in message or 'Too Many Requests' in message


def parse_json3(text: str) -> list[dict]:
    segments: list[dict] = []
    data = json.loads(text)
    for event in data.get("events", []):
        raw_text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
        raw_text = unescape(re.sub(r"\s+", " ", raw_text).strip())
        if not raw_text:
            continue
        segments.append(
            {
                "start": (event.get("tStartMs", 0) or 0) / 1000,
                "dur": (event.get("dDurationMs", 0) or 0) / 1000,
                "text": raw_text,
            }
        )
    return segments


def parse_xml(text: str) -> list[dict]:
    segments: list[dict] = []
    matches = list(re.finditer(r'<text[^>]*start="([^"]*)"[^>]*dur="([^"]*)"[^>]*>([\s\S]*?)</text>', text))
    if not matches:
        matches = list(re.finditer(r'<text[^>]*t="([^"]*)"[^>]*d="([^"]*)"[^>]*>([\s\S]*?)</text>', text))
        for match in matches:
            start = int(match.group(1)) / 1000
            dur = int(match.group(2)) / 1000
            caption = unescape(re.sub(r'<[^>]+>', ' ', match.group(3)))
            caption = re.sub(r'\s+', ' ', caption).strip()
            if caption:
                segments.append({"start": start, "dur": dur, "text": caption})
        return segments

    for match in matches:
        start = float(match.group(1))
        dur = float(match.group(2))
        caption = unescape(re.sub(r'<[^>]+>', ' ', match.group(3)))
        caption = re.sub(r'\s+', ' ', caption).strip()
        if caption:
            segments.append({"start": start, "dur": dur, "text": caption})
    return segments


def parse_vtt(text: str) -> list[dict]:
    segments: list[dict] = []
    blocks = re.split(r'\n\s*\n', text.replace('\r\n', '\n').strip())
    for block in blocks:
        lines = [line.strip() for line in block.split('\n') if line.strip()]
        if len(lines) < 2 or '-->' not in lines[0]:
            continue
        match = re.match(r'(?P<start>\d{2}:\d{2}:\d{2}\.\d{3})\s+-->\s+(?P<end>\d{2}:\d{2}:\d{2}\.\d{3})', lines[0])
        if not match:
            continue
        start = _parse_vtt_timestamp(match.group('start'))
        end = _parse_vtt_timestamp(match.group('end'))
        caption = unescape(' '.join(lines[1:]))
        caption = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', caption)).strip()
        if caption:
            segments.append({"start": start, "dur": max(0.5, end - start), "text": caption})
    return segments


def _parse_vtt_timestamp(value: str) -> float:
    hours, minutes, seconds = value.split(':')
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_caption_payload(text: str) -> list[dict]:
    payload = (text or '').strip()
    if not payload:
        return []
    if payload.startswith('{'):
        return parse_json3(payload)
    if payload.startswith('WEBVTT') or '-->' in payload:
        return parse_vtt(payload)
    return parse_xml(payload)


def choose_language(info: dict, lang: str) -> tuple[str | None, dict | None]:
    subtitles = info.get('subtitles') or {}
    automatic = info.get('automatic_captions') or {}
    candidates: list[str] = []
    base_lang = lang.split('-')[0]

    def pick_track(candidate: str) -> tuple[str, dict] | None:
        if candidate in subtitles and subtitles[candidate]:
            return candidate, subtitles[candidate][0]
        if candidate in automatic and automatic[candidate]:
            return candidate, automatic[candidate][0]
        return None

    if lang == 'original':
        if subtitles:
            first = sorted(subtitles.keys())[0]
            return first, subtitles[first][0]
        if automatic:
            first = sorted(automatic.keys())[0]
            return first, automatic[first][0]
        return None, None

    if lang == 'hinglish':
        hinglish_order = ['en-IN', 'en', 'hi', 'hi-IN']
        all_keys = sorted(set(list(subtitles.keys()) + list(automatic.keys())))
        for key in all_keys:
            if key.startswith('en') and key not in hinglish_order:
                hinglish_order.append(key)
        for key in all_keys:
            if key.startswith('hi') and key not in hinglish_order:
                hinglish_order.append(key)
        for key in all_keys:
            if key not in hinglish_order:
                hinglish_order.append(key)

        for key in hinglish_order:
            picked = pick_track(key)
            if picked:
                return picked
        return None, None

    def add(value: str | None) -> None:
        if value and value not in candidates:
            candidates.append(value)

    if lang == 'en':
        add('en-IN')
    elif lang.startswith('en-'):
        add('en')
    add(lang)
    if base_lang != lang:
        add(base_lang)

    keys = list(subtitles.keys()) + list(automatic.keys())
    if lang == 'en':
        for key in keys:
            if key.startswith('en'):
                add(key)
    elif lang.startswith('en-'):
        for key in keys:
            if key.startswith('en'):
                add(key)
    else:
        for key in keys:
            if key == lang or key.startswith(f'{lang}-'):
                add(key)
        for key in keys:
            if key == base_lang or key.startswith(f'{base_lang}-'):
                add(key)

    for candidate in candidates:
        picked = pick_track(candidate)
        if picked:
            return picked

    return None, None


def fetch_transcript(video_url: str, lang: str) -> dict:
    video_id = extract_video_id(video_url)
    if video_id:
        cached = read_cache(video_id, lang)
        if cached and cached.get('segments'):
            return cached

        if lang in {'original', 'hinglish'}:
            any_cached = read_any_cache(video_id)
            if any_cached and any_cached.get('segments'):
                return any_cached

    try:
        with YoutubeDL({'skip_download': True, 'writesubtitles': True, 'writeautomaticsubs': True, 'quiet': True, 'no_warnings': True}) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as exc:
        if is_rate_limited_error(exc):
            raise RuntimeError('Too many requests from YouTube right now. Please wait a minute and try again.') from exc
        raise

    title = info.get('title') or 'YouTube Video'
    chosen_lang, track = choose_language(info, lang)
    if not track:
        return {'title': title, 'lang': chosen_lang, 'segments': []}

    try:
        data = urlopen(track['url']).read().decode('utf-8', 'replace')
    except Exception as exc:
        if is_rate_limited_error(exc):
            raise RuntimeError('Too many requests from YouTube right now. Please wait a minute and try again.') from exc
        raise
    segments = parse_caption_payload(data)

    payload = {'title': title, 'lang': chosen_lang, 'segments': segments}

    if video_id and segments:
        write_cache(video_id, lang, payload)
        if chosen_lang and chosen_lang != lang:
            write_cache(video_id, chosen_lang, payload)

    return payload


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == '/health':
            self._send_json(200, {'ok': True})
            return

        if parsed.path == '/' or parsed.path == '/youtube-transcript.html':
            self._send_file(ROOT_DIR / 'youtube-transcript.html', 'text/html; charset=utf-8')
            return

        if parsed.path == '/transcript1.txt':
            self._send_file(ROOT_DIR / 'transcript1.txt', 'text/plain; charset=utf-8')
            return

        if parsed.path != '/transcript':
            self._send_json(404, {'error': 'not found'})
            return

        params = parse_qs(parsed.query)
        video_url = (params.get('url') or [''])[0]
        lang = (params.get('lang') or ['en'])[0]
        if not video_url:
            self._send_json(400, {'error': 'missing url'})
            return

        try:
            payload = fetch_transcript(video_url, lang)
            self._send_json(200, payload)
        except Exception as exc:
            message = str(exc)
            if '429' in message or 'Too Many Requests' in message:
                self._send_json(429, {'error': 'Too many requests from YouTube right now. Please wait a minute and try again.'})
                return
            self._send_json(500, {'error': message})

    def _send_file(self, file_path: Path, content_type: str) -> None:
        if not file_path.exists():
            self._send_json(404, {'error': 'file not found'})
            return

        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Transcript proxy listening on http://{args.host}:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
