# 台灣輿情情報系統

- `index.html` — 投料編輯台與情報輸出區（接 Make Webhook）
- `extract.py` — **原型**：YouTube 連結 → 逐字稿 → 人事時地物（5W1H）情報 JSON

## extract.py 原型

```
YT URL → yt-dlp metadata → 官方/自動字幕（缺才 ASR）→ Gemini 抽 5W → JSON →（可選）POST webhook
```

### 安裝

```bash
pip install -r requirements.txt
# ASR 後備（沒字幕的影片才需要）：
#   pip install faster-whisper   並安裝 ffmpeg
export GEMINI_API_KEY=你的金鑰
```

### 使用

```bash
# 1. 先只看逐字稿，確認字幕品質
python extract.py "https://www.youtube.com/watch?v=XXXX" --transcript-only

# 2. 產出人事時地物 JSON
python extract.py "https://www.youtube.com/watch?v=XXXX" --out result.json

# 3. 直接送進現有 Make 中文情報 Webhook
python extract.py URL --webhook "https://hook.make.com/xxxx"

# 沒字幕的影片，給 ASR 專有名詞詞庫提高正確率
python extract.py URL --hotwords "賴清德,國防部,漢光演習"

# 已有逐字稿（例如自己聽打），跳過抓取
python extract.py URL --transcript-file my_transcript.txt
```

### 輸出結構

```json
{
  "sourceName": "YouTube",
  "url": "...",
  "video": { "video_id": "", "title": "", "channel": "", "published_at": "", "duration_sec": 0 },
  "transcript_source": "official_sub | auto_sub | asr | file",
  "intel": {
    "summary": "",
    "who":    [{ "name": "", "role": "", "quote": "", "ts": "12:34" }],
    "what":   [""],
    "when":   [{ "text": "", "ts": "" }],
    "where":  [{ "place": "", "ts": "" }],
    "things": [{ "item": "", "detail": "", "ts": "" }],
    "claims": [{ "text": "", "by": "", "type": "fact|claim|opinion", "verifiable": true, "ts": "" }],
    "stance": "",
    "confidence": 0.0,
    "needs_verification": [""],
    "note": ""
  },
  "referenceText": "帶時間碼的逐字稿（對應編輯台的參考文字欄位）"
}
```

`intel` 內每個關鍵點都帶時間碼，方便回頭核對原片；`claims` 區分事實／主張／評論，
`needs_verification` 是應人工查證的項目，可對應輸出區的待查證標示。

### 下一步（原型驗證後）

1. 頻道 RSS 監看（`https://www.youtube.com/feeds/videos.xml?channel_id=...`）＋ 已處理清單去重，定時自動跑
2. 把 `claims` 送進現有 Gemini 全網雷同檢查 webhook
3. 情報存檔與列表視圖
