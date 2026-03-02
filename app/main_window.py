from __future__ import annotations

import queue
import re
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import requests

from PyQt6.QtCore import QEvent, QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QClipboard, QGuiApplication, QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db import Artwork, ArtworkDb
from app.dezoomify import download_via_dezoomify
from app.metadata import ASSET_PREFIX_NO_TRAILING, fetch_asset_metadata, is_asset_url
from app.paths import data_dir, downloads_dir, thumbs_dir


class _MetadataCollectorThread(QThread):
    progress = pyqtSignal(str)
    done = pyqtSignal(int)
    error = pyqtSignal(str)

    def __init__(self, *, db_path: Path, parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = db_path
        self._q: queue.Queue[str | None] = queue.Queue()

    def enqueue(self, url: str) -> None:
        self._q.put(url)
        if not self.isRunning():
            self.start()

    def shutdown(self) -> None:
        self.requestInterruption()
        self._q.put(None)

    def run(self) -> None:
        # Run a simple blocking loop; queue.Queue is thread-safe.
        while True:
            if self.isInterruptionRequested():
                return
            url = self._q.get()
            if url is None:
                return

            try:
                self.progress.emit("메타 수집 중...")
                md = fetch_asset_metadata(url)

                self.progress.emit("DB 저장 중...")
                db = ArtworkDb(self._db_path)
                artwork_id = db.upsert_artwork(
                    asset_url=md.asset_url,
                    title=md.title,
                    creator=md.creator,
                    year=md.year,
                    description=md.description,
                    thumbnail_url=md.thumbnail_url,
                )

                if md.thumbnail_url:
                    # Best-effort thumbnail cache.
                    try:
                        self.progress.emit("썸네일 저장 중...")
                        resp = requests.get(md.thumbnail_url, timeout=15)
                        resp.raise_for_status()
                        p = thumbs_dir() / f"{artwork_id}.jpg"
                        p.write_bytes(resp.content)
                    except Exception:
                        pass

                self.progress.emit("메타 저장 완료")
                self.done.emit(artwork_id)
            except Exception as e:
                self.error.emit(f"{type(e).__name__}: {e}")


def _safe_filename(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    # Windows filename constraints.
    s = re.sub(r"[\\/:*?\"<>|]", "-", s)
    s = s.strip(" .")
    return s or "untitled"


def _display_scaled_size(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return width, height
    scale = 2160 / height
    new_width = int(round(width * scale))
    new_height = 2160
    if new_width > 3840:
        scale = 3840 / width
        new_width = 3840
        new_height = int(round(height * scale))
    return new_width, new_height


def _resize_for_display(src_path: Path, dst_path: Path) -> None:
    # Dezoomify 초고해상도 이미지는 디코딩 시 256MB 기본 제한을 초과할 수 있으므로 상향
    QImageReader.setAllocationLimit(1024)
    image = QImage(str(src_path))
    if image.isNull():
        raise ValueError("이미지 로드에 실패했습니다.")
    new_width, new_height = _display_scaled_size(image.width(), image.height())
    if new_width <= 0 or new_height <= 0:
        raise ValueError("이미지 해상도를 계산할 수 없습니다.")
    resized = image.scaled(
        new_width,
        new_height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    if not resized.save(str(dst_path), "JPG", quality=100):
        raise ValueError("변경된 이미지 저장에 실패했습니다.")


@dataclass
class DownloadQueueItem:
    artwork_id: int


class _DezoomifyWorker(QObject):
    progress = pyqtSignal(str)
    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, *, asset_url: str, temp_path: Path, dest_path: Path):
        super().__init__()
        self._asset_url = asset_url
        self._temp_path = temp_path
        self._dest_path = dest_path

    @pyqtSlot()
    def run(self) -> None:
        try:
            self.progress.emit("Dezoomify 처리 시작...")
            if self._temp_path.exists():
                self._temp_path.unlink()
            res = download_via_dezoomify(
                self._asset_url,
                self._temp_path,
                headless=False,
                temp_downloads_dir=(data_dir() / "playwright_downloads"),
            )
            if res.saved_path != self._dest_path:
                self._dest_path.parent.mkdir(parents=True, exist_ok=True)
                if self._dest_path.exists():
                    self._dest_path.unlink()
                res.saved_path.rename(self._dest_path)

            changed_path = self._dest_path.with_name(f"[changed]{self._dest_path.name}")
            _resize_for_display(self._dest_path, changed_path)
            self.done.emit(str(changed_path))
        except Exception as e:
            self.error.emit(f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Arts & Culture Collector")
        self.resize(1100, 700)

        self._db_path = data_dir() / "artworks.sqlite3"
        self._db = ArtworkDb(self._db_path)
        self._clipboard = cast(QClipboard, QGuiApplication.clipboard())
        self._last_clipboard_text = ""

        self._meta_thread = _MetadataCollectorThread(db_path=self._db_path, parent=self)

        self._download_queue: list[DownloadQueueItem] = []
        self._active_download: DownloadQueueItem | None = None
        self._error_count: int = 0
        self._pw_thread: QThread | None = None
        self._pw_worker: _DezoomifyWorker | None = None
        self._last_checked_row: int | None = None
        self._all_checked: bool = False
        self._no_sort_asc: bool | None = None  # None=미정렬, True=오름차순, False=내림차순
        self._active_artwork: Artwork | None = None  # 현재 다운로드 중인 작품 (로그용)

        self._build_ui()

        self._meta_thread.progress.connect(self.clipboard_status.setText)
        self._meta_thread.done.connect(self._on_meta_saved)
        self._meta_thread.error.connect(self._on_meta_error)

        self._wire_clipboard()
        self._reload_table()

    # -----------------
    # UI
    # -----------------

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        top = QHBoxLayout()
        outer.addLayout(top)

        self.open_site_btn = QPushButton("Google Arts & Culture 열기")
        self.open_site_btn.clicked.connect(self._open_google_arts)
        top.addWidget(self.open_site_btn)

        self.paste_btn = QPushButton("URL 붙여넣기")
        self.paste_btn.clicked.connect(self._paste_from_clipboard)
        top.addWidget(self.paste_btn)

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(f"{ASSET_PREFIX_NO_TRAILING} 로 시작하는 링크를 붙여넣으세요")
        top.addWidget(self.url_input, 1)

        self.add_btn = QPushButton("메타 수집/저장")
        self.add_btn.clicked.connect(self._collect_from_input)
        top.addWidget(self.add_btn)

        self.clipboard_status = QLabel("클립보드 감시: ON")
        top.addWidget(self.clipboard_status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        left = QWidget()
        left_l = QVBoxLayout(left)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "다운로드",
            "No.",
            "제목",
            "작가",
            "연도",
            "링크",
        ])
        vh = self.table.verticalHeader()
        if vh is not None:
            vh.setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.viewport().installEventFilter(self)

        hdr = self.table.horizontalHeader()
        if hdr is not None:
            hdr.sectionClicked.connect(self._on_header_clicked)

        left_l.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        left_l.addLayout(btn_row)

        self.refresh_btn = QPushButton("선택 메타 재수집")
        self.refresh_btn.clicked.connect(self._refresh_selected)
        btn_row.addWidget(self.refresh_btn)

        self.delete_btn = QPushButton("선택 삭제")
        self.delete_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.delete_btn)

        self.open_asset_btn = QPushButton("선택 링크 열기")
        self.open_asset_btn.clicked.connect(self._open_selected_asset)
        btn_row.addWidget(self.open_asset_btn)

        splitter.addWidget(left)

        right = QWidget()
        right_l = QVBoxLayout(right)

        details = QGroupBox("상세")
        d_l = QVBoxLayout(details)

        self.thumb = QLabel()
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setMinimumHeight(220)
        d_l.addWidget(self.thumb)

        self.detail_title = QLabel("제목: ")
        self.detail_creator = QLabel("작가: ")
        self.detail_year = QLabel("연도: ")
        self.detail_url = QLabel("링크: ")
        self.detail_url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        d_l.addWidget(self.detail_title)
        d_l.addWidget(self.detail_creator)
        d_l.addWidget(self.detail_year)
        d_l.addWidget(self.detail_url)

        self.detail_desc = QTextEdit()
        self.detail_desc.setReadOnly(True)
        d_l.addWidget(self.detail_desc, 1)

        right_l.addWidget(details, 1)

        dl = QGroupBox("다운로드 워크플로")
        dl_l = QVBoxLayout(dl)

        self.output_dir_label = QLabel()
        dl_l.addWidget(self.output_dir_label)

        self.start_queue_btn = QPushButton("선택한 그림 다운로드 큐 시작")
        self.start_queue_btn.clicked.connect(self._start_download_queue)
        dl_l.addWidget(self.start_queue_btn)

        self.queue_status = QLabel("큐: 대기")
        dl_l.addWidget(self.queue_status)

        right_l.addWidget(dl)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self._update_output_dir_label()

    # -----------------
    # Event filter
    # -----------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        """우클릭이 row selection을 변경하지 않도록 차단한다."""
        if obj is self.table.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                from PyQt6.QtGui import QMouseEvent
                me = event  # type: ignore[assignment]
                if isinstance(me, QMouseEvent) and me.button() == Qt.MouseButton.RightButton:
                    return True  # 이벤트 소비 → Qt 기본 selection 변경 차단
        return super().eventFilter(obj, event)

    def _wire_clipboard(self) -> None:
        self._clipboard.dataChanged.connect(self._on_clipboard_changed)

    def _update_output_dir_label(self) -> None:
        self.output_dir_label.setText(f"저장 폴더: {downloads_dir()}")

    # -----------------
    # Actions
    # -----------------
    def _open_google_arts(self) -> None:
        webbrowser.open("https://artsandculture.google.com/category/artist?hl=ko")

    def _paste_from_clipboard(self) -> None:
        self.url_input.setText(self._clipboard.text().strip())

    def _collect_from_input(self) -> None:
        url = self.url_input.text().strip()
        self._collect_and_store(url)

    def _on_clipboard_changed(self) -> None:
        txt = self._clipboard.text().strip()
        if not txt or txt == self._last_clipboard_text:
            return
        self._last_clipboard_text = txt
        if is_asset_url(txt):
            self.url_input.setText(txt)
            self._collect_and_store(txt)

    def _collect_and_store(self, url: str) -> None:
        if not is_asset_url(url):
            return
        self.clipboard_status.setText("메타 수집 대기...")
        self._meta_thread.enqueue(url)

    def _on_meta_saved(self, artwork_id: int) -> None:
        self._reload_table(select_id=artwork_id)

    def _on_meta_error(self, msg: str) -> None:
        # Avoid modal dialogs here to keep continuous clipboard flow.
        self.clipboard_status.setText(f"메타 수집 실패: {msg}")

    def closeEvent(self, event) -> None:  # type: ignore[override]
        try:
            self._meta_thread.shutdown()
            self._meta_thread.wait(1500)
        except Exception:
            pass
        super().closeEvent(event)

    def _fetch_and_cache_thumb(self, artwork_id: int, thumb_url: str) -> None:
        # Best-effort thumbnail cache.
        try:
            resp = requests.get(thumb_url, timeout=15)
            resp.raise_for_status()
            p = thumbs_dir() / f"{artwork_id}.jpg"
            p.write_bytes(resp.content)
        except Exception:
            return

    def _reload_table(self, *, select_id: int | None = None) -> None:
        self._artworks = self._db.list_artworks()
        if self._no_sort_asc is not None:
            self._artworks = sorted(
                self._artworks,
                key=lambda a: a.seq_no,
                reverse=not self._no_sort_asc,
            )
        self.table.setRowCount(0)
        for row_idx, a in enumerate(self._artworks):
            self.table.insertRow(row_idx)

            chk = QTableWidgetItem("")
            chk.setFlags(chk.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            chk.setCheckState(Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, a.id)
            self.table.setItem(row_idx, 0, chk)

            self.table.setItem(row_idx, 1, QTableWidgetItem(str(a.seq_no)))
            self.table.setItem(row_idx, 2, QTableWidgetItem(a.title))
            self.table.setItem(row_idx, 3, QTableWidgetItem(a.creator))
            self.table.setItem(row_idx, 4, QTableWidgetItem(a.year))
            self.table.setItem(row_idx, 5, QTableWidgetItem(a.asset_url))

        self.table.resizeColumnsToContents()
        if select_id is not None:
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if not item:
                    continue
                if item.data(Qt.ItemDataRole.UserRole) == select_id:
                    self.table.selectRow(r)
                    break

    def _selected_artwork_ids(self) -> list[int]:
        ids: list[int] = []
        sm = self.table.selectionModel()
        if sm is None:
            return ids
        for idx in sm.selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids

    def _checked_artwork_ids(self) -> list[int]:
        ids: list[int] = []
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                ids.append(chk.data(Qt.ItemDataRole.UserRole))
        return ids

    def _on_header_clicked(self, section: int) -> None:
        if section == 0:
            # 체크박스 전체 선택/해제
            self._all_checked = not self._all_checked
            state = Qt.CheckState.Checked if self._all_checked else Qt.CheckState.Unchecked
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item:
                    item.setCheckState(state)

        elif section == 1:
            # No. 컬럼: 오름차순 ↔ 내림차순 toggle
            self._no_sort_asc = not self._no_sort_asc if self._no_sort_asc is not None else True
            self._reload_table()
            self._update_no_header_label()

    def _update_no_header_label(self) -> None:
        """No. 컬럼 헤더에 정렬 방향 표시 (▲ 오름차순 / ▼ 내림차순)."""
        if self._no_sort_asc is True:
            label = "No. ▲"
        elif self._no_sort_asc is False:
            label = "No. ▼"
        else:
            label = "No."
        self.table.setHorizontalHeaderItem(1, QTableWidgetItem(label))

    def _on_cell_clicked(self, row: int, col: int) -> None:
        modifiers = QGuiApplication.keyboardModifiers()

        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            if self._last_checked_row is not None:
                start = min(self._last_checked_row, row)
                end = max(self._last_checked_row, row)
                for r in range(start, end + 1):
                    item = self.table.item(r, 0)
                    if item:
                        item.setCheckState(Qt.CheckState.Checked)
            else:
                item = self.table.item(row, 0)
                if item:
                    item.setCheckState(Qt.CheckState.Checked)
            self._last_checked_row = row
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            if col != 0:
                item = self.table.item(row, 0)
                if item:
                    new_state = (
                        Qt.CheckState.Unchecked
                        if item.checkState() == Qt.CheckState.Checked
                        else Qt.CheckState.Checked
                    )
                    item.setCheckState(new_state)
            self._last_checked_row = row
        else:
            # checkbox 컬럼이 아닌 제목 등 다른 컬럼 클릭 시에도 anchor를 갱신해야
            # 이후 Shift+클릭의 range 기준이 올바르게 동작한다.
            self._last_checked_row = row

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        """제목(col=2) 더블클릭 시 해당 그림의 링크를 브라우저 새 탭에서 연다."""
        if col != 2:
            return
        chk = self.table.item(row, 0)
        if chk is None:
            return
        artwork_id = chk.data(Qt.ItemDataRole.UserRole)
        a = self._db.get_artwork(artwork_id)
        if a:
            webbrowser.open_new_tab(a.asset_url)

    def _on_selection_changed(self) -> None:
        ids = self._selected_artwork_ids()
        if not ids:
            return
        a = self._db.get_artwork(ids[0])
        if not a:
            return

        self.detail_title.setText(f"제목: {a.title}")
        self.detail_creator.setText(f"작가: {a.creator}")
        self.detail_year.setText(f"연도: {a.year}")
        self.detail_url.setText(f"링크: {a.asset_url}")
        self.detail_desc.setPlainText(a.description)

        thumb_path = thumbs_dir() / f"{a.id}.jpg"
        if thumb_path.exists():
            pm = QPixmap(str(thumb_path))
            if not pm.isNull():
                self.thumb.setPixmap(pm.scaledToHeight(220, Qt.TransformationMode.SmoothTransformation))
                return
        self.thumb.setPixmap(QPixmap())

    def _refresh_selected(self) -> None:
        for artwork_id in self._selected_artwork_ids():
            a = self._db.get_artwork(artwork_id)
            if a:
                self._collect_and_store(a.asset_url)

    def _delete_selected(self) -> None:
        ids = self._checked_artwork_ids()
        if not ids:
            QMessageBox.information(self, "삭제", "삭제할 그림을 먼저 체크하세요.")
            return

        reply = QMessageBox.question(
            self,
            "삭제 확인",
            f"{len(ids)}개의 항목을 정말 삭제하시겠습니까?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.No:
            return

        self._db.delete_artworks(ids)
        for artwork_id in ids:
            p = thumbs_dir() / f"{artwork_id}.jpg"
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass
        self._reload_table()

    def _open_selected_asset(self) -> None:
        ids = self._selected_artwork_ids()
        if not ids:
            return
        a = self._db.get_artwork(ids[0])
        if a:
            webbrowser.open(a.asset_url)

    def _start_download_queue(self) -> None:
        checked = self._checked_artwork_ids()
        if not checked:
            QMessageBox.information(self, "안내", "다운로드할 그림을 체크하세요.")
            return
        self._download_queue = [DownloadQueueItem(i) for i in checked]
        self._total_in_queue = len(self._download_queue)
        self._processed_count = 0
        self._error_count = 0
        self._active_download = None
        self._advance_queue()

    def _output_path_for_artwork(self, a: Artwork) -> Path:
        out_dir = downloads_dir()
        title = _safe_filename(a.title)
        creator = _safe_filename(a.creator)
        year = _safe_filename(a.year)
        out_name = f"{title}-{creator}-{year}.jpg" if creator or year else f"{title}.jpg"
        dst = out_dir / out_name
        if dst.exists():
            dst = out_dir / f"{title}-{creator}-{year}-{a.id}.jpg"
        return dst

    def _advance_queue(self) -> None:
        if self._active_download is not None:
            return
        if not self._download_queue:
            if self._error_count:
                summary = f"큐: 완료 (성공 {self._total_in_queue - self._error_count}건, 실패 {self._error_count}건)"
                self.queue_status.setText(summary)
                QMessageBox.information(self, "다운로드", summary)
            else:
                self.queue_status.setText("큐: 완료")
                QMessageBox.information(self, "다운로드", "다운로드 큐가 완료되었습니다.")
            return

        self._active_download = self._download_queue.pop(0)
        self._processed_count += 1
        a = self._db.get_artwork(self._active_download.artwork_id)
        if not a:
            self._active_download = None
            self._active_artwork = None
            self._advance_queue()
            return

        self._active_artwork = a
        dst = self._output_path_for_artwork(a)
        self.queue_status.setText(f"진행 중 ({self._processed_count}/{self._total_in_queue}): {a.title}")
        self._start_playwright_download(a.asset_url, dst)

    def _start_playwright_download(self, asset_url: str, dst: Path) -> None:
        # Ensure prior worker is cleaned up.
        if self._pw_thread is not None:
            try:
                self._pw_thread.quit()
                if not self._pw_thread.wait(5000):
                    self._pw_thread.terminate()
                    self._pw_thread.wait(3000)
            except Exception:
                pass

        thread = QThread(self)
        temp_path = downloads_dir() / "dezoomify-result.jpg"
        worker = _DezoomifyWorker(
            asset_url=asset_url,
            temp_path=temp_path,
            dest_path=dst,
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self.queue_status.setText)
        worker.done.connect(self._on_playwright_done)
        worker.error.connect(self._on_playwright_error)

        worker.done.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._pw_thread = thread
        self._pw_worker = worker
        thread.start()

    def _log_download(self, *, success: bool, msg: str = "") -> None:
        """다운로드 결과를 data/download_log.txt 에 기록한다."""
        from datetime import datetime
        a = self._active_artwork
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        status = "SUCCESS" if success else "FAIL   "
        if a:
            line = f"[{timestamp}] {status} | No.{a.seq_no:<4} | {a.title} | {a.asset_url}"
        else:
            line = f"[{timestamp}] {status} | (unknown artwork)"
        if not success and msg:
            line += f" | {msg}"
        print(line, flush=True)
        log_path = data_dir() / "download_log.txt"
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[LOG WRITE ERROR] {e}", flush=True)

    def _on_playwright_done(self, saved_path: str) -> None:
        self._log_download(success=True)
        self.queue_status.setText(f"저장 완료: {saved_path}")
        self._active_download = None
        self._active_artwork = None
        self._advance_queue()

    def _on_playwright_error(self, msg: str) -> None:
        self._error_count = getattr(self, "_error_count", 0) + 1
        self._log_download(success=False, msg=msg)
        self.queue_status.setText(
            f"다운로드 실패 ({self._error_count}건 스킵): {msg[:80]}"
        )
        self._active_download = None
        self._active_artwork = None
        self._advance_queue()

    def _play_completion_sound_10s(self) -> None:
        # Windows-friendly minimal sound without extra assets.
        try:
            import winsound

            end_ms = 10_000
            step_ms = 250
            freq = 880
            for _ in range(end_ms // step_ms):
                winsound.Beep(freq, step_ms)
                freq = 660 if freq == 880 else 880
        except Exception:
            return
