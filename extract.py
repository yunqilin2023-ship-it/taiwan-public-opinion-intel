#!/usr/bin/env python3
"""
YouTube → 逐字稿 → 人事時地物（5W1H）情報 JSON  —  原型

流程：
  1. yt-dlp 抓影片 metadata
  2. 優先抓 YouTube 官方/自動字幕（zh-Hant > zh > en）
  3. 沒字幕才下載音訊，交給 Whisper 做語音辨識（可選）
  4. 把帶時間碼的逐字稿丟給 Gemini，用固定 schema 抽出 who/what/when/where/things/claims
  5. 印出 JSON；加 --webhook 可直接 POST 到現有 Make Webhook

用法：
  export GEMINI_API_KEY=...
  python extract.py "https://www.youtube.com/watch?v=XXXX"
  python extract.py URL --webhook "https://hook.make.com/..." --out result.json
  python extract.py URL --transcript-only        # 只看逐字稿，先驗字幕品質
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
SUB_LANG_PRIORITY = ["zh-Hant", "zh-TW", "zh", "zh-Hans", "en"]

# 逐字稿太長時截斷（字元數），避免超過模型上下文；原型階段夠用。
MAX_TRANSCRIPT_CHARS = 60_000

EXTRACT_PROMPT = """你是一位台灣輿情情報分析員。下面是一支 YouTube 影片的逐字稿（每行前面是時間碼）。
請從逐字稿抽出「人事時地物」情報，並以 JSON 輸出，**只輸出 JSON，不要任何說明文字**。

硬規則：
1. 只根據逐字稿內容作答，逐字稿沒提到的一律留空或空陣列，**絕對不要腦補**。
2. 嚴格區分「事實陳述」、「當事人主張」、「主持人/來賓評論」。主張與評論放進 claims，並標記是否可查證。
3. 每個關鍵點都附上出現的時間碼（ts），格式沿用逐字稿的時間碼。
4. when 要區分「影片發布時間」與「逐字稿中提到的事件時間」，事件時間用逐字稿原話（例如「上週」「9 月 3 日」），不要自行換算成絕對日期。
5. 人名、單位名、地名用逐字稿出現的寫法；若明顯是 ASR 錯字且你有把握，才在 note 標註可能的正確寫法。
6. needs_verification 列出應該人工查證的項目（沒說出處的數字、單一來源的指控、無法從逐字稿確認的說法）。

輸出 schema：
{
  "summary": "三句以內的事件摘要",
  "who":   [{"name": "", "role": "身分/單位", "quote": "關鍵發言原句", "ts": ""}],
  "what":  ["發生了什麼事，一條一件"],
  "when":  [{"text": "逐字稿原話的時間描述", "ts": ""}],
  "where": [{"place": "", "ts": ""}],
  "things":[{"item": "法案/金額/裝備/文件/代號等", "detail": "", "ts": ""}],
  "claims":[{"text": "", "by": "誰說的", "type": "fact|claim|opinion", "verifiable": true, "ts": ""}],
  "stance": "整體立場傾向與語氣，一句話",
  "confidence": 0.0,
  "needs_verification": [""],
  "note": ""
}

影片資訊：
- 頻道：{channel}
- 標題：{title}
- 發布時間：{published}

逐字稿：
{transcript}
"""


# ---------------------------------------------------------------------------
# ① yt-dlp：metadata 與字幕
# ---------------------------------------------------------------------------
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def fetch_metadata(url: str) -> dict:
    proc = run(["yt-dlp", "--dump-single-json", "--skip-download", url])
    if proc.returncode != 0:
        sys.exit(f"[yt-dlp] 抓 metadata 失敗：\n{proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    return {
        "video_id": info.get("id"),
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "published_at": _fmt_upload_date(info.get("upload_date")),
        "duration_sec": info.get("duration"),
        "subtitles": list((info.get("subtitles") or {}).keys()),
        "auto_captions": list((info.get("automatic_captions") or {}).keys()),
    }


def _fmt_upload_date(s: str | None) -> str | None:
    if not s or len(s) != 8:
        return s
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def pick_sub_lang(meta: dict) -> tuple[str | None, bool]:
    """回傳 (語言代碼, 是否為自動字幕)。

    先比語言（原文中文優先，保留人名/單位原始寫法），同語言內官方字幕優先於自動字幕。
    """
    for lang in SUB_LANG_PRIORITY:
        if lang in meta["subtitles"]:
            return lang, False
        if lang in meta["auto_captions"]:
            return lang, True
    return None, False


def fetch_subtitles(url: str, lang: str, auto: bool, workdir: Path) -> str | None:
    flag = "--write-auto-subs" if auto else "--write-subs"
    proc = run(
        [
            "yt-dlp", flag, "--sub-langs", lang, "--sub-format", "vtt",
            "--skip-download", "-o", str(workdir / "sub.%(ext)s"), url,
        ]
    )
    if proc.returncode != 0:
        print(f"[yt-dlp] 抓字幕失敗：{proc.stderr.strip()[:300]}", file=sys.stderr)
        return None
    files = list(workdir.glob("sub*.vtt"))
    return files[0].read_text(encoding="utf-8", errors="ignore") if files else None


def vtt_to_transcript(vtt: str) -> str:
    """把 VTT 壓成『[mm:ss] 文字』一行一句，並去掉自動字幕常見的重複行。"""
    lines: list[str] = []
    last_text = ""
    ts = None
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        m = re.match(r"(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->", line)
        if m:
            h, mi, s = m.groups()
            ts = f"{int(h) * 60 + int(mi):02d}:{s}"
            continue
        if re.match(r"^\d+$", line):
            continue
        text = re.sub(r"<[^>]+>", "", line).strip()
        if not text or text == last_text:
            continue
        last_text = text
        lines.append(f"[{ts or '00:00'}] {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ② ASR 後備：沒字幕時下載音訊，用 faster-whisper（若有安裝）
# ---------------------------------------------------------------------------
def fetch_audio(url: str, workdir: Path) -> Path | None:
    proc = run(
        ["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "5",
         "-o", str(workdir / "audio.%(ext)s"), url]
    )
    if proc.returncode != 0:
        print(f"[yt-dlp] 下載音訊失敗：{proc.stderr.strip()[:300]}", file=sys.stderr)
        return None
    files = list(workdir.glob("audio.*"))
    return files[0] if files else None


def asr_transcribe(audio: Path, hotwords: str | None) -> str | None:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print(
            "[ASR] 沒有字幕，且未安裝 faster-whisper。\n"
            "      pip install faster-whisper  之後重跑即可自動轉文字；\n"
            f"      或手動轉好逐字稿後用 --transcript-file 餵進來。音訊已存在：{audio}",
            file=sys.stderr,
        )
        return None
    model_size = os.environ.get("WHISPER_MODEL", "large-v3")
    print(f"[ASR] faster-whisper {model_size} 轉文字中…", file=sys.stderr)
    model = WhisperModel(model_size, compute_type="auto")
    segments, _ = model.transcribe(
        str(audio), language="zh", vad_filter=True,
        initial_prompt=hotwords,  # 專有名詞詞庫，提高台灣人名/單位正確率
    )
    lines = []
    for seg in segments:
        m, s = divmod(int(seg.start), 60)
        lines.append(f"[{m:02d}:{s:02d}] {seg.text.strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ③ Gemini：抽人事時地物
# ---------------------------------------------------------------------------
def extract_5w(meta: dict, transcript: str) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("請先設定環境變數 GEMINI_API_KEY")

    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        print(f"[warn] 逐字稿 {len(transcript)} 字，截斷到 {MAX_TRANSCRIPT_CHARS}", file=sys.stderr)
        transcript = transcript[:MAX_TRANSCRIPT_CHARS]

    # 提示詞裡的 schema 含大括號，不能用 str.format，改逐一替換
    prompt = EXTRACT_PROMPT
    for k, v in {
        "channel": meta.get("channel"), "title": meta.get("title"),
        "published": meta.get("published_at"), "transcript": transcript,
    }.items():
        prompt = prompt.replace("{" + k + "}", str(v))
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    resp = requests.post(
        GEMINI_URL.format(model=GEMINI_MODEL, key=key), json=body, timeout=180
    )
    if resp.status_code != 200:
        sys.exit(f"[Gemini] HTTP {resp.status_code}: {resp.text[:500]}")
    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    return json.loads(text)


# ---------------------------------------------------------------------------
# ④ 組裝與輸出
# ---------------------------------------------------------------------------
def build_payload(meta: dict, intel: dict, transcript: str, source: str) -> dict:
    """輸出格式對齊 index.html 投料編輯台的欄位，可直接 POST 進現有 webhook。"""
    return {
        "sourceName": "YouTube",
        "url": meta["url"],
        "video": {
            "video_id": meta["video_id"], "title": meta["title"],
            "channel": meta["channel"], "channel_id": meta["channel_id"],
            "published_at": meta["published_at"], "duration_sec": meta["duration_sec"],
        },
        "transcript_source": source,      # official_sub / auto_sub / asr / file
        "intel": intel,
        "referenceText": transcript,      # 對應編輯台的「參考文字」欄位
    }


def post_webhook(url: str, payload: dict) -> None:
    r = requests.post(url, json=payload, timeout=60)
    print(f"[webhook] {r.status_code} {r.text[:200]}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube → 人事時地物情報 JSON 原型")
    ap.add_argument("url", help="YouTube 影片連結")
    ap.add_argument("--out", help="把結果 JSON 寫到檔案")
    ap.add_argument("--webhook", help="把結果 POST 到 Make Custom Webhook")
    ap.add_argument("--transcript-only", action="store_true", help="只輸出逐字稿，不呼叫 Gemini")
    ap.add_argument("--transcript-file", help="跳過抓字幕/ASR，直接用這份逐字稿")
    ap.add_argument("--no-asr", action="store_true", help="沒字幕時不做 ASR，直接結束")
    ap.add_argument("--hotwords", help="ASR 專有名詞詞庫（逗號分隔），例如：賴清德,國防部,漢光演習")
    args = ap.parse_args()

    meta = fetch_metadata(args.url)
    print(f"[meta] {meta['channel']}｜{meta['title']}｜{meta['published_at']}", file=sys.stderr)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        transcript, source = None, None

        if args.transcript_file:
            transcript = Path(args.transcript_file).read_text(encoding="utf-8")
            source = "file"
        else:
            lang, auto = pick_sub_lang(meta)
            if lang:
                print(f"[sub] 使用{'自動' if auto else '官方'}字幕 {lang}", file=sys.stderr)
                vtt = fetch_subtitles(args.url, lang, auto, workdir)
                if vtt:
                    transcript = vtt_to_transcript(vtt)
                    source = "auto_sub" if auto else "official_sub"
            if not transcript and not args.no_asr:
                print("[sub] 無可用字幕，改走 ASR", file=sys.stderr)
                audio = fetch_audio(args.url, workdir)
                if audio:
                    transcript = asr_transcribe(audio, args.hotwords)
                    source = "asr"

    if not transcript:
        sys.exit("拿不到逐字稿，結束。")

    if args.transcript_only:
        print(transcript)
        return

    intel = extract_5w(meta, transcript)
    payload = build_payload(meta, intel, transcript, source)
    out = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"[out] 已寫入 {args.out}", file=sys.stderr)
    else:
        print(out)

    if args.webhook:
        post_webhook(args.webhook, payload)


if __name__ == "__main__":
    main()
