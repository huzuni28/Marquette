import sys
from dataclasses import dataclass
from typing import List, Optional, Callable

import fitz  # PyMuPDF

from PySide6.QtCore import Qt, QRect, QPoint, QSize, QObject, QEvent
from PySide6.QtGui import (
    QImage, QPixmap, QKeySequence, QAction, QFont, QPainter, QColor, QPen, QBrush, QIcon
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QMessageBox,
    QScrollArea, QLabel, QWidget, QLineEdit,
    QToolBar, QFontComboBox, QSpinBox, QMenu, QToolButton, QRubberBand,
    QSplitter, QListWidget, QListWidgetItem, QVBoxLayout,
    QSlider
)


# ---------- Icons ----------

def make_red_trash_icon(size: int = 16) -> QIcon:
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

    body = QRect(4, 5, size - 8, size - 7)
    p.drawRoundedRect(body, 2, 2)

    p.setBrush(QBrush(red.lighter(110)))
    p.drawRoundedRect(QRect(3, 3, size - 6, 4), 2, 2)

    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRect(size // 2 - 3, 1, 6, 3), 2, 2)

    p.setPen(QPen(QColor(255, 220, 220, 200), 1))
    for x in (size // 2 - 3, size // 2, size // 2 + 3):
        p.drawLine(x, 6, x, size - 4)

    p.end()
    return QIcon(pm)


def make_text_tool_icon(size: int = 18, color: QColor = QColor(30, 30, 30)) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    pen = QPen(color)
    pen.setWidth(2)
    p.setPen(pen)

    # "T"
    p.drawLine(size // 4, 4, size - size // 4, 4)
    p.drawLine(size // 2, 4, size // 2, size - 4)

    p.end()
    return QIcon(pm)


# ---------- Font mapping (annotation-safe) ----------

def qfont_to_pdf_basefont(qfont: QFont) -> str:
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


# ---------- Widgets ----------

class ResizeHandle(QLabel):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self.setStyleSheet("background: rgba(0,0,0,120);")
        self.setCursor(Qt.SizeFDiagCursor)


class TextBoxWidget(QWidget):
    MIN_W = 60
    MIN_H = 28
    BORDER_GRAB_PX = 7

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

        self._moving = False
        self._move_start_global = QPoint()
        self._start_pos = QPoint()

        self.setGeometry(self.model.rect_px)
        self._layout_children()

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

    def _is_on_border(self, p: QPoint) -> bool:
        r = self.rect()
        near_left = p.x() <= self.BORDER_GRAB_PX
        near_right = p.x() >= r.width() - self.BORDER_GRAB_PX
        near_top = p.y() <= self.BORDER_GRAB_PX
        near_bottom = p.y() >= r.height() - self.BORDER_GRAB_PX
        return near_left or near_right or near_top or near_bottom

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_selected(self)

            if self.handle.geometry().contains(event.position().toPoint()):
                self._resizing = True
                self._resize_start_global = event.globalPosition().toPoint()
                self._start_geom = self.geometry()
                event.accept()
                return

            if self._is_on_border(event.position().toPoint()):
                self._moving = True
                self._move_start_global = event.globalPosition().toPoint()
                self._start_pos = self.pos()
                self.setCursor(Qt.SizeAllCursor)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing:
            delta = event.globalPosition().toPoint() - self._resize_start_global
            new_w = max(self.MIN_W, self._start_geom.width() + delta.x())
            new_h = max(self.MIN_H, self._start_geom.height() + delta.y())

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

        if self._moving:
            delta = event.globalPosition().toPoint() - self._move_start_global
            new_pos = self._start_pos + delta

            parent = self.parentWidget()
            if parent:
                new_x = max(0, min(new_pos.x(), parent.width() - self.width()))
                new_y = max(0, min(new_pos.y(), parent.height() - self.height()))
                new_pos = QPoint(new_x, new_y)

            self.move(new_pos)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._resizing:
                self._resizing = False
                self._commit_geom_change()
                event.accept()
                return
            if self._moving:
                self._moving = False
                self.unsetCursor()
                self._commit_geom_change()
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def _commit_geom_change(self):
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

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        act_del = QAction(self._trash_icon, "Delete", self)
        act_del.triggered.connect(lambda: self._on_request_delete(self))
        menu.addAction(act_del)
        menu.exec(event.globalPos())

    def _on_editing_finished(self):
        txt = self.edit.text().strip()
        if txt == "":
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


class PageView(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        self.page_index = 0
        self.zoom = 2.0
        self._image_w_px = 0
        self._image_h_px = 0

        self.current_qfont = QFont("Arial")
        self.current_fontsize = 12

        self.trash_icon = make_red_trash_icon(16)
        self.boxes: List[TextBoxWidget] = []
        self.selected: Optional[TextBoxWidget] = None
        self.undo = UndoStack()

        self.text_tool_enabled = False
        self._dragging = False
        self._drag_start = QPoint()
        self._rubber = QRubberBand(QRubberBand.Rectangle, self)
        self._rubber.hide()

        self.default_click_w = 360
        self.default_click_h = 30

    def set_text_tool(self, enabled: bool):
        self.text_tool_enabled = enabled
        self.setCursor(Qt.IBeamCursor if enabled else Qt.ArrowCursor)
        if not enabled and self._dragging:
            self._dragging = False
            self._rubber.hide()

    def set_font_settings(self, qfont: QFont, fontsize: int):
        self.current_qfont = QFont(qfont)
        self.current_fontsize = int(fontsize)
        if self.selected:
            before = self.selected.model
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

    def spawn_default_box_left_middle(self):
        if self._image_w_px <= 0 or self._image_h_px <= 0:
            return
        x = 30
        y = max(0, (self._image_h_px // 2) - (self.default_click_h // 2))
        w = min(self.default_click_w, self._image_w_px - x)
        h = self.default_click_h
        if w < 80:
            return
        self._create_box(QRect(x, y, w, h), treat_as_drag_rect=False)

    def _create_box(self, rect: QRect, treat_as_drag_rect: bool = True):
        if treat_as_drag_rect:
            r = rect.normalized()
            if r.width() < 40 and r.height() < 20:
                x = r.x()
                y = r.y() - (self.default_click_h // 2)
                y = max(0, min(y, self._image_h_px - self.default_click_h))
                max_w = self._image_w_px - x
                w = min(self.default_click_w, max_w)
                if w < 40:
                    return
                r = QRect(x, y, w, self.default_click_h)
            else:
                x0 = max(0, min(r.left(), self._image_w_px - 1))
                y0 = max(0, min(r.top(), self._image_h_px - 1))
                x1 = max(0, min(r.right(), self._image_w_px - 1))
                y1 = max(0, min(r.bottom(), self._image_h_px - 1))
                r = QRect(QPoint(x0, y0), QPoint(x1, y1)).normalized()

                w = max(TextBoxWidget.MIN_W, r.width())
                h = max(TextBoxWidget.MIN_H, r.height())
                w = min(w, self._image_w_px - r.x())
                h = min(h, self._image_h_px - r.y())
                r = QRect(r.x(), r.y(), w, h)
        else:
            r = rect

        model = BoxModel(
            page_index=self.page_index,
            rect_px=r,
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
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        child = self.childAt(event.position().toPoint())
        if child and isinstance(child.parentWidget(), TextBoxWidget):
            self.selected = child.parentWidget()
            return super().mousePressEvent(event)

        if not self.text_tool_enabled:
            return super().mousePressEvent(event)

        self._dragging = True
        self._drag_start = event.position().toPoint()
        self._rubber.setGeometry(QRect(self._drag_start, QSize(1, 1)))
        self._rubber.show()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._dragging:
            curr = event.position().toPoint()
            self._rubber.setGeometry(QRect(self._drag_start, curr).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._dragging and event.button() == Qt.LeftButton:
            self._dragging = False
            self._rubber.hide()
            end = event.position().toPoint()
            self._create_box(QRect(self._drag_start, end), treat_as_drag_rect=True)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CtrlWheelZoomFilter(QObject):
    def __init__(self, on_zoom_delta: Callable[[int], None]):
        super().__init__()
        self._on_zoom_delta = on_zoom_delta

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel:
            if QApplication.keyboardModifiers() & Qt.ControlModifier:
                self._on_zoom_delta(event.angleDelta().y())
                return True
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Marquette Alpha")

        self._doc: Optional[fitz.Document] = None
        self._path: Optional[str] = None
        self._page_index = 0

        self.thumbs = QListWidget()
        self.thumbs.setIconSize(QSize(140, 180))
        self.thumbs.setMinimumWidth(190)
        self.thumbs.currentRowChanged.connect(self._on_thumb_selected)

        self.page_view = PageView()
        self.page_container = QWidget()
        lay = QVBoxLayout(self.page_container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
        lay.addWidget(self.page_view)

        self.scroll = QScrollArea()
        self.scroll.setWidget(self.page_container)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)

        self.splitter = QSplitter()
        self.splitter.addWidget(self.thumbs)
        self.splitter.addWidget(self.scroll)
        self.splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.splitter)

        self._zoom = 2.0
        self.fit_width = True

        self._ctrl_wheel_filter = CtrlWheelZoomFilter(self._on_ctrl_wheel_zoom)
        self.scroll.viewport().installEventFilter(self._ctrl_wheel_filter)

        self._build_toolbar()
        self._build_status_zoom_slider()
        self._build_actions()

        self.thumbs_visible = True
        self._apply_dynamic_icons()

        self.resize(1300, 850)

    # --- Theme / icon coloring ---

    def _is_dark_mode(self) -> bool:
        bg = self.palette().window().color()
        luminance = (0.2126 * bg.red() + 0.7152 * bg.green() + 0.0722 * bg.blue())
        return luminance < 128

    def _apply_dynamic_icons(self):
        color = QColor(240, 240, 240) if self._is_dark_mode() else QColor(20, 20, 20)
        self.text_tool_btn.setIcon(make_text_tool_icon(18, color=color))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.ApplicationPaletteChange):
            # safe: no global event filter recursion
            self._apply_dynamic_icons()

    # --- Toolbar / Status ---

    def _build_toolbar(self):
        tb = QToolBar("Tools", self)
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

        self.text_tool_btn = QToolButton(self)
        self.text_tool_btn.setCheckable(True)
        self.text_tool_btn.setChecked(False)
        self.text_tool_btn.setToolTip("Text tool")
        self.text_tool_btn.toggled.connect(self._on_text_tool_toggled)
        self.text_tool_btn.clicked.connect(self._on_text_tool_clicked_spawn)
        tb.addWidget(self.text_tool_btn)

        tb.addSeparator()

        self.toggle_thumbs_btn = QToolButton(self)
        self.toggle_thumbs_btn.setCheckable(True)
        self.toggle_thumbs_btn.setChecked(True)
        self.toggle_thumbs_btn.setText("Pages")
        self.toggle_thumbs_btn.setToolTip("Show/hide page previews")
        self.toggle_thumbs_btn.toggled.connect(self.set_thumbnails_visible)
        tb.addWidget(self.toggle_thumbs_btn)

    def _build_status_zoom_slider(self):
        self.statusBar().showMessage("Ready")

        self.zoom_slider = QSlider(Qt.Horizontal, self)
        self.zoom_slider.setRange(25, 400)
        self.zoom_slider.setValue(200)
        self.zoom_slider.setFixedWidth(220)
        self.zoom_slider.setToolTip("Zoom (%)")
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider_changed)

        self.zoom_percent_label = QLabel("200%", self)

        self.statusBar().addPermanentWidget(QLabel("Zoom:", self))
        self.statusBar().addPermanentWidget(self.zoom_slider)
        self.statusBar().addPermanentWidget(self.zoom_percent_label)

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

        menu = self.menuBar()
        filem = menu.addMenu("File")
        filem.addAction(open_act)
        filem.addAction(save_act)

        editm = menu.addMenu("Edit")
        editm.addAction(undo_act)
        editm.addAction(redo_act)

        for a in (open_act, save_act, undo_act, redo_act):
            self.addAction(a)

    # --- Thumbnails show/hide ---

    def set_thumbnails_visible(self, visible: bool):
        self.thumbs_visible = visible
        self.thumbs.setVisible(visible)

        total = max(1, self.splitter.width())
        if visible:
            self.splitter.setSizes([220, total - 220])
        else:
            self.splitter.setSizes([0, total])

    # --- Text tool ---

    def _on_text_tool_toggled(self, enabled: bool):
        self.page_view.set_text_tool(enabled)

    def _on_text_tool_clicked_spawn(self):
        if self.text_tool_btn.isChecked():
            self.page_view.spawn_default_box_left_middle()

    # --- Zoom ---

    def _update_zoom_percent_label(self):
        self.zoom_percent_label.setText(f"{int(round(self._zoom * 100))}%")

    def _set_zoom(self, zoom: float, update_slider: bool = True):
        self.fit_width = False
        self._zoom = max(0.25, min(4.0, float(zoom)))
        if update_slider:
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(int(round(self._zoom * 100)))
            self.zoom_slider.blockSignals(False)
        self._update_zoom_percent_label()
        if self._doc:
            self.render_current_page()

    def _on_zoom_slider_changed(self, value: int):
        self._set_zoom(value / 100.0, update_slider=False)

    def _on_ctrl_wheel_zoom(self, delta_y: int):
        step = 10
        if delta_y > 0:
            self.zoom_slider.setValue(min(self.zoom_slider.maximum(), self.zoom_slider.value() + step))
        else:
            self.zoom_slider.setValue(max(self.zoom_slider.minimum(), self.zoom_slider.value() - step))

    def _compute_fit_width_zoom(self, page: fitz.Page) -> float:
        viewport_w = max(1, self.scroll.viewport().width() - 30)
        page_w_pt = page.rect.width
        z = viewport_w / page_w_pt
        return max(0.25, min(4.0, float(z)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._doc and self.fit_width:
            self.render_current_page()

    # --- Fonts ---

    def _on_font_change(self, qfont: QFont):
        self.page_view.set_font_settings(qfont, self.size_box.value())

    def _on_size_change(self, size: int):
        self.page_view.set_font_settings(self.font_box.currentFont(), size)

    # --- Thumbnails ---

    def _build_thumbnails(self):
        self.thumbs.blockSignals(True)
        self.thumbs.clear()

        if not self._doc:
            self.thumbs.blockSignals(False)
            return

        for i in range(self._doc.page_count):
            page = self._doc.load_page(i)
            mat = fitz.Matrix(0.22, 0.22)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
            icon = QIcon(QPixmap.fromImage(img.copy()))
            item = QListWidgetItem(icon, f"{i+1}")
            item.setSizeHint(QSize(170, 200))
            self.thumbs.addItem(item)

        self.thumbs.setCurrentRow(self._page_index)
        self.thumbs.blockSignals(False)

    def _on_thumb_selected(self, row: int):
        if not self._doc:
            return
        if row < 0 or row >= self._doc.page_count:
            return
        self._page_index = row
        self.render_current_page()

    # --- PDF open/render ---

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
        self.fit_width = True
        self._build_thumbnails()
        self.render_current_page()

    def render_current_page(self):
        if not self._doc:
            return

        page = self._doc.load_page(self._page_index)
        if self.fit_width:
            self._zoom = self._compute_fit_width_zoom(page)
            self.zoom_slider.blockSignals(True)
            self.zoom_slider.setValue(int(round(self._zoom * 100)))
            self.zoom_slider.blockSignals(False)

        self._update_zoom_percent_label()

        mat = fitz.Matrix(self._zoom, self._zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888)
        qpix = QPixmap.fromImage(img.copy())
        self.page_view.set_rendered_page(qpix, self._page_index, self._zoom)

        if self.thumbs.currentRow() != self._page_index:
            self.thumbs.setCurrentRow(self._page_index)

    # --- Save into same PDF (FreeText annotations) ---

    def _style_freetext_plain(self, annot, qfont: QFont, fontsize: int):
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
