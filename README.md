# Arts & Culture Collector (PyQt)

Clipboard-driven helper to collect metadata for Google Arts & Culture `asset` pages and manage a local library.

Important: you are responsible for complying with copyrights and site Terms of Service.

## Install

```bash
python -m pip install -r requirements.txt
```

Playwright needs a browser binary (Chromium):

```bash
python -m playwright install chromium
```

## Run

```bash
python main.py
```

## Usage

1. Click `Google Arts & Culture 열기`.
2. In your browser, navigate to an artwork page and copy its URL.
   - Expected format: `https://artsandculture.google.com/asset/...`
3. The app detects the clipboard URL, fetches metadata, and stores it in `data/artworks.sqlite3`.
4. Check multiple rows in the `다운로드` column to build a queue.
5. Click `선택한 그림 다운로드 큐 시작`.
   - The app opens a separate Chromium window (ko-KR locale), enters the URL into `https://dezoomify.ophir.dev/`, and clicks `Save image` automatically.
   - Output is saved to `./download/{title}-{creator}-{year}.jpg`.

## Notes

- Thumbnail caching: stored under `data/thumbs/`.
