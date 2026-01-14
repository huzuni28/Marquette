import sys
from dataclasses import dataclass
from typing import List, Optional, Callable

import fitz  # PyMuPDF

from PySide6.QtCore import Qt, QRect, QPoint, QSize
from PySide6.QtGui import (
    QImage, QPixmap, QKeySequence, QAction, QFont, QPainter, QColor, QPen, QBrush, QIcon
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QMessageBox,
    QScrollArea, QLabel, QWidget, QLineEdit,
    QToolBar, QFontComboBox, QSpinBox, QMenu
)


# ---------- Helpers ----------

def make_red_trash_icon(size: int = 16) -> QIcon:
    """Create a small red trash-can icon (no external assets)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    red = QColor(220, 0, 0)
    dark = QColor(140, 0, 0)

    pen = QPen(dark)
    pen.setWidth(2)
    p.setPen(pen)
    p.setBrush(QBrush(red))

    # Body
    body = QRect(4, 5, size - 8, size - 7)
    p.drawRoundedRect(body, 2, 2)

    # Lid
    p.setBrush(QBrush(red.lighter(110)))
    p.drawRoundedRect(QRect(3, 3, size - 6, 4), 2, 2)

    # Handle
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRect(size // 2 - 3, 1, 6, 3), 2, 2)

    # Slats
    p.setPen(QPen(QColor(255, 220, 220, 200), 1))
    for x in (size // 2 - 3, size // 2, size // 2 + 3):
        p.drawLine(x, 6, x, size - 4)

    p.end()
    return QIcon(pm)


def qfont_to_pdf_basefont(qfont: QFont) -> str:
    """
    PDF annotations reliably support base-14 fonts across viewers.
    Map chosen font family to one of:
      helv (Helvetica), tiro (Times), cour (Courier)
    """
    fam = (qfont.family() or "").lower()
    if any(k in fam for k in ["courier", "consolas", "mono", "dejavu sans mono", "liberation mono"]):
        return "cour"
    if any(k in fam for k in ["times", "serif", "dejavu serif", "liberation serif"]):
        return "tiro"
    return "helv"


# ---------- Data model ----------

@dataclass
class BoxModel:
    page_index: int
    rect_px: QRect
    text: str
    qfont: QFont
    fontsize_pt: int


# ---------- Undo/Redo ----------

class Command:
    def __init__(self, do: Callable[[], None], undo: Callable[[], None], name: str = ""):
        self._do = do
        self._undo = undo
        self.name = name

    def do(self):
        self._do()

    def undo(self):
        self._undo()


class UndoStack:
    def __init__(self):
        self._undo: List[Command] = []
        self._redo: List[Command] = []

    def push_and_do(self, cmd: Command):
        cmd.do()
        self._undo.append(cmd)
        self._redo.clear()

    def undo(self):
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)

    def redo(self):
        if not self._redo:
            return
        cmd = self._redo.pop()
        cmd.do()
        self._undo.append(cmd)


# ---------- UI widgets ----------

class ResizeHandle(QLabel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.setStyleSheet("background: rgba(0,0,0,120);")
        self.setCursor(Qt.SizeFDiagCursor)


class TextBoxWidget(QWidget):
    """
    Persistent text box overlay:
    - QLineEdit for typing
    - resizable via bottom-right handle
    - right-click context menu with red trash icon
    - auto-delete if text is empty when editing finishes
    """
    MIN_W = 60
    MIN_H = 28

    def __init__(
        self,
        parent: QWidget,
        model: BoxModel,
        trash_icon: QIcon,
        on_selected: Callable[['TextBoxWidget'], None],
        on_change_commit: Callable[['TextBoxWidget', BoxModel, BoxModel], None],
        on_request_delete: Callable[['TextBoxWidget'], None],
    ):
        super().__init__(parent)
        self.model = model
        self._trash_icon = trash_icon

        self._on_selected = on_selected
        self._on_change_commit = on_change_commit
        self._on_request_delete = on_request_delete

        self.edit = QLineEdit(self)
        self.edit.setFrame(False)
        self.edit.setStyleSheet(
            "background: rgba(255,255,255,230);"
            "color: black;"
            "border: 1px solid rgba(0,0,0,90);"
            "padding: 2px;"
        )
        self.edit.setText(model.text)
        qf = QFont(model.qfont)
        qf.setPointSize(model.fontsize_pt)
        self.edit.setFont(qf)

        self.handle = ResizeHandle(self)

        self._resizing = False
        self._resize_start_global = QPoint()
        self._start_geom = QRect()

        # apply geometry
        self.setGeometry(self.model.rect_px)
        self._layout_children()

        # commit changes / auto-delete
        self.edit.editingFinished.connect(self._on_editing_finished)

        self.setMouseTracking(True)

    def _layout_children(self):
        r = self.rect()
        self.edit.setGeometry(0, 0, r.width(), r.height())
        self.handle.move(r.width() - self.handle.width(), r.height() - self.handle.height())

    def set_model(self, model: BoxModel):
        self.model = model
        self.edit.setText(model.text)
        qf = QFont(model.qfont)
        qf.setPointSize(model.fontsize_pt)
        self.edit.setFont(qf)
        self.setGeometry(model.rect_px)
        self._layout_children()

    def focusInEvent(self, event):
        self._on_selected(self)
        super().focusInEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_selected(self)

            # resizing if on handle
            if self.handle.geometry().contains(event.position().toPoint()):
                self._resizing = True
                self._resize_start_global = event.globalPosition().toPoint()
                self._start_geom = self.geometry()
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.globalPosition().toPoint() - self._resize_start_global
            new_w = max(self.MIN_W, self._start_geom.width() + delta.x())
            new_h = max(self.MIN_H, self._start_geom.height() + delta.y())

            # clamp to parent bounds
            parent = self.parentWidget()
            if parent:
                max_w = parent.width() - self._start_geom.x()
                max_h = parent.height() - self._start_geom.y()
                new_w = min(new_w, max_w)
                new_h = min(new_h, max_h)

            self.setGeometry(self._start_geom.x(), self._start_geom.y(), new_w, new_h)
            self._layout_children()
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._resizing and event.button() == Qt.LeftButton:
            self._resizing = False

            before = BoxModel(
                page_index=self.model.page_index,
                rect_px=QRect(self.model.rect_px),
                text=self.model.text,
                qfont=QFont(self.model.qfont),
                fontsize_pt=self.model.fontsize_pt,
            )
            after = BoxModel(
                page_index=self.model.page_index,
                rect_px=QRect(self.geometry()),
                text=self.edit.text(),
                qfont=QFont(self.model.qfont),
                fontsize_pt=self.model.fontsize_pt,
            )
            self.model = after
            self._on_change_commit(self, before, after)

            event.accept()
            return

        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_del = QAction(self._trash_icon, "Delete", self)
        act_del.triggered.connect(lambda: self._on_request_delete(self))
        menu.addAction(act_del)
        menu.exec(event.globalPos())

    def _on_editing_finished(self):
        txt = self.edit.text().strip()
        if txt == "":
            # auto delete empty boxes
            self._on_request_delete(self)
            return

        if txt != self.model.text:
            before = BoxModel(
                page_index=self.model.page_index,
                rect_px=QRect(self.model.rect_px),
                text=self.model.text,
                qfont=QFont(self.model.qfont),
                fontsize_pt=self.model.fontsize_pt,
            )
            after = BoxModel(
                page_index=self.model.page_index,
                rect_px=QRect(self.geometry()),
                text=txt,
                qfont=QFont(self.model.qfont),
                fontsize_pt=self.model.fontsize_pt,
            )
            self.model = after
            self._on_change_commit(self, before, after)


# ---------- Page view ----------

class PageView(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.page_index = 0
        self.zoom = 2.0
        self._image_w_px = 0
        self._image_h_px = 0

        self.default_box_w = 360
        self.default_box_h = 30

        self.current_qfont = QFont("Arial")
        self.current_fontsize = 12

        self.trash_icon = make_red_trash_icon(16)

        self.boxes: List[TextBoxWidget] = []
        self.selected: Optional[TextBoxWidget] = None
        self.undo = UndoStack()

    def set_font_settings(self, qfont: QFont, fontsize: int):
        self.current_qfont = QFont(qfont)
        self.current_fontsize = int(fontsize)

        # Apply to selected box only (Preview-like behavior)
        if self.selected:
            before = BoxModel(
                page_index=self.selected.model.page_index,
                rect_px=QRect(self.selected.model.rect_px),
                text=self.selected.model.text,
                qfont=QFont(self.selected.model.qfont),
                fontsize_pt=self.selected.model.fontsize_pt,
            )
            after = BoxModel(
                page_index=self.selected.model.page_index,
                rect_px=QRect(self.selected.geometry()),
                text=self.selected.edit.text(),
                qfont=QFont(self.current_qfont),
                fontsize_pt=self.current_fontsize,
            )

            def do():
                self.selected.set_model(after)

            def undo():
                self.selected.set_model(before)

            self.undo.push_and_do(Command(do, undo, "Change Font"))

    def set_rendered_page(self, qpix: QPixmap, page_index: int, zoom: float):
        self.setPixmap(qpix)
        self._image_w_px = qpix.width()
        self._image_h_px = qpix.height()
        self.page_index = page_index
        self.zoom = zoom
        self.resize(qpix.size())

        for b in self.boxes:
            b.setVisible(b.model.page_index == self.page_index)

    def px_rect_to_pdf_rect(self, rect_px: QRect) -> fitz.Rect:
        x0 = rect_px.left() / self.zoom
        y0 = rect_px.top() / self.zoom
        x1 = rect_px.right() / self.zoom
        y1 = rect_px.bottom() / self.zoom
        return fitz.Rect(x0, y0, x1, y1)

    # callbacks from boxes
    def _on_selected(self, box: TextBoxWidget):
        self.selected = box

    def _on_change_commit(self, box: TextBoxWidget, before: BoxModel, after: BoxModel):
        def do():
            box.set_model(after)

        def undo():
            box.set_model(before)

        self.undo.push_and_do(Command(do, undo, "Update Box"))

    def _request_delete(self, box: TextBoxWidget):
        if box not in self.boxes:
            return

        idx = self.boxes.index(box)
        before_model = box.model

        def do():
            if box in self.boxes:
                self.boxes.remove(box)
            box.hide()
            if self.selected == box:
                self.selected = None

        def undo():
            self.boxes.insert(idx, box)
            box.set_model(before_model)
            box.show()
            box.raise_()

        self.undo.push_and_do(Command(do, undo, "Delete Box"))

    def add_box_at_click(self, click_pos: QPoint):
        # Preview-like: box starts at click.x (caret at front) and extends right
        x = click_pos.x()
        y = click_pos.y() - (self.default_box_h // 2)
        y = max(0, min(y, self._image_h_px - self.default_box_h))

        max_w = self._image_w_px - x
        w = min(self.default_box_w, max_w)
        if w < 40:
            return

        rect = QRect(x, y, w, self.default_box_h)
        model = BoxModel(
            page_index=self.page_index,
            rect_px=rect,
            text="",
            qfont=QFont(self.current_qfont),
            fontsize_pt=self.current_fontsize,
        )

        box = TextBoxWidget(
            parent=self,
            model=model,
            trash_icon=self.trash_icon,
            on_selected=self._on_selected,
            on_change_commit=self._on_change_commit,
            on_request_delete=self._request_delete,
        )
        box.setVisible(True)

        def do():
            self.boxes.append(box)
            box.show()
            box.raise_()
            self.selected = box
            box.edit.setFocus()
            box.edit.setCursorPosition(0)

        def undo():
            if box in self.boxes:
                self.boxes.remove(box)
            box.hide()
            if self.selected == box:
                self.selected = None

        self.undo.push_and_do(Command(do, undo, "Add Box"))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # If click is on existing box, let it handle focus
            child = self.childAt(event.position().toPoint())
            if child and isinstance(child.parentWidget(), TextBoxWidget):
                self.selected = child.parentWidget()
                return super().mousePressEvent(event)

            self.add_box_at_click(event.position().toPoint())
            return

        super().mousePressEvent(event)


# ---------- Main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Preview-like PDF Text Boxes (Resizable + Undo/Redo + Right-click Delete)")

        self._doc: Optional[fitz.Document] = None
        self._path: Optional[str] = None
        self._page_index = 0
        self._zoom = 2.0

        self.page_view = PageView()
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.page_view)
        self.scroll.setWidgetResizable(False)
        self.setCentralWidget(self.scroll)

        self._build_toolbar()
        self._build_actions()
        self.resize(1200, 820)

    def _build_toolbar(self):
        tb = QToolBar("Text", self)
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        self.font_box = QFontComboBox(self)
        self.font_box.setCurrentFont(QFont("Arial"))
        self.font_box.currentFontChanged.connect(self._on_font_change)
        tb.addWidget(self.font_box)

        self.size_box = QSpinBox(self)
        self.size_box.setRange(6, 96)
        self.size_box.setValue(12)
        self.size_box.valueChanged.connect(self._on_size_change)
        tb.addWidget(self.size_box)

    def _on_font_change(self, qfont: QFont):
        self.page_view.set_font_settings(qfont, self.size_box.value())

    def _on_size_change(self, size: int):
        self.page_view.set_font_settings(self.font_box.currentFont(), size)

    def _build_actions(self):
        open_act = QAction("Open…", self)
        open_act.setShortcut(QKeySequence.Open)
        open_act.triggered.connect(self.open_pdf)

        save_act = QAction("Save", self)
        save_act.setShortcut(QKeySequence.Save)
        save_act.triggered.connect(self.save_pdf)

        undo_act = QAction("Undo", self)
        undo_act.setShortcut(QKeySequence.Undo)
        undo_act.triggered.connect(self.page_view.undo.undo)

        redo_act = QAction("Redo", self)
        redo_act.setShortcut(QKeySequence.Redo)
        redo_act.triggered.connect(self.page_view.undo.redo)

        next_act = QAction("Next Page", self)
        next_act.setShortcut(Qt.Key_PageDown)
        next_act.triggered.connect(lambda: self.go_page(self._page_index + 1))

        prev_act = QAction("Prev Page", self)
        prev_act.setShortcut(Qt.Key_PageUp)
        prev_act.triggered.connect(lambda: self.go_page(self._page_index - 1))

        zoom_in = QAction("Zoom In", self)
        zoom_in.setShortcut(QKeySequence.ZoomIn)
        zoom_in.triggered.connect(lambda: self.set_zoom(self._zoom * 1.25))

        zoom_out = QAction("Zoom Out", self)
        zoom_out.setShortcut(QKeySequence.ZoomOut)
        zoom_out.triggered.connect(lambda: self.set_zoom(self._zoom / 1.25))

        menu = self.menuBar()
        filem = menu.addMenu("File")
        filem.addAction(open_act)
        filem.addAction(save_act)

        editm = menu.addMenu("Edit")
        editm.addAction(undo_act)
        editm.addAction(redo_act)

        viewm = menu.addMenu("View")
        viewm.addAction(prev_act)
        viewm.addAction(next_act)
        viewm.addSeparator()
        viewm.addAction(zoom_in)
        viewm.addAction(zoom_out)

        # register shortcuts globally
        for a in (open_act, save_act, undo_act, redo_act):
            self.addAction(a)

    def open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF", "", "PDF files (*.pdf)")
        if not path:
            return
        try:
            self._doc = fitz.open(path)
        except Exception as e:
            QMessageBox.critical(self, "Open failed", str(e))
            return

        self._path = path
        self._page_index = 0
        self.render_current_page()

    def set_zoom(self, zoom: float):
        if not self._doc:
            return
        self._zoom = max(0.5, min(6.0, zoom))
        self.render_current_page()

    def go_page(self, idx: int):
        if not self._doc:
            return
        idx = max(0, min(self._doc.page_count - 1, idx))
        if idx == self._page_index:
            return
        self._page_index = idx
        self.render_current_page()

    def render_current_page(self):
        if not self._doc:
            return
        page = self._doc.load_page(self._page_index)
        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)

        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        qpix = QPixmap.fromImage(img.copy())

        self.page_view.set_rendered_page(qpix, self._page_index, self._zoom)

    def _style_freetext_plain(self, annot, qfont: QFont, fontsize: int):
        # no border / no fill
        try:
            annot.set_border(width=0)
        except Exception:
            pass
        try:
            annot.set_colors(stroke=None, fill=None)
        except Exception:
            pass
        try:
            annot.set_opacity(1)
        except Exception:
            pass

        # default appearance: base-14 font mapping + size + black
        pdf_font = qfont_to_pdf_basefont(qfont)
        try:
            annot.set_default_appearance(fontname=pdf_font, fontsize=int(fontsize), text_color=(0, 0, 0))
        except Exception:
            pass

        try:
            annot.update()
        except Exception:
            pass

    def save_pdf(self):
        if not self._path:
            return

        try:
            doc = fitz.open(self._path)

            # Write boxes as FreeText annotations (standard PDF), skip empties.
            for box in self.page_view.boxes:
                text = box.edit.text().strip()
                if not text:
                    continue

                model = box.model
                page = doc.load_page(model.page_index)
                rect_pt = self.page_view.px_rect_to_pdf_rect(model.rect_px)

                annot = page.add_freetext_annot(rect_pt, text)
                self._style_freetext_plain(annot, model.qfont, model.fontsize_pt)

            doc.saveIncr()
            doc.close()
            QMessageBox.information(self, "Saved", "Saved into the same PDF (Ctrl+S).")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
