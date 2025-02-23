import sys
import os
import re
import json
import logging
import subprocess

import pandas as pd
import validators

# For better fuzzy matching:
try:
    from rapidfuzz import fuzz, process
except ImportError:
    # pip install rapidfuzz
    fuzz = None
    process = None

from PyQt6.QtCore import (
    Qt,
    QThread,
    pyqtSignal,
    pyqtSlot,
    QObject,
    QRect
)
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QAbstractItemView,
    QProgressBar,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QSpinBox
)

###############################################################################
# LOGGING SETUP
###############################################################################
logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

###############################################################################
# CONFIG
###############################################################################
CONFIG_FILE = "config.json"

def load_config() -> dict:
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error reading config: {e}", exc_info=True)
    return {
        "lastRegex": "",
        "theme": "Light",
        "columnsToKeep": [],
        "columnsToValidate": [],
        "lastOutputBaseName": "cleaned_output",
        "windowGeometry": None,
        "numThreads": 4,
        "outputDir": ""
    }

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        logging.info("Config saved.")
    except Exception as e:
        logging.error(f"Error saving config: {e}", exc_info=True)

###############################################################################
# VALIDATION
###############################################################################
def default_is_valid_url(url: str) -> bool:
    if pd.isna(url) or not url.strip():
        return False
    return validators.url(url) == True

def regex_is_valid_url(url: str, pattern: str) -> bool:
    if pd.isna(url) or not url.strip():
        return False
    return bool(re.match(pattern, url.strip()))

###############################################################################
# FILE IO
###############################################################################
def write_file(df: pd.DataFrame, output_path: str, header: bool = True, mode: str = 'w'):
    ext = os.path.splitext(output_path)[1].lower()
    if ext == '.csv':
        df.to_csv(output_path, index=False, header=header, mode=mode, encoding='utf-8-sig')
    elif ext in ['.xlsx', '.xls']:
        df.to_excel(output_path, index=False)
    else:
        raise ValueError(f"Unsupported output file type: '{ext}'")

def chunked_csv_reader(input_path: str, chunksize: int = 50000, encoding="utf-8-sig"):
    for chunk in pd.read_csv(input_path, chunksize=chunksize, encoding=encoding):
        yield chunk

def read_preview_df(file_path: str, nrows: int = 5) -> pd.DataFrame:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.csv':
        return pd.read_csv(file_path, nrows=nrows, encoding='utf-8-sig')
    elif ext in ['.xlsx', '.xls']:
        return pd.read_excel(file_path, nrows=nrows)
    else:
        raise ValueError(f"Unsupported file type for preview: {ext}")

###############################################################################
# WORKER: Process a Single File
###############################################################################
class CleanDataWorker(QObject):
    """
    Processes a single file. We'll assign tasks from a queue to an available worker.
    """
    # progress(rowIndex, progressValue)
    progress = pyqtSignal(int, int)
    # finished(rowIndex, message)
    finished = pyqtSignal(int, str)
    # error(rowIndex, errorMessage)
    error = pyqtSignal(int, str)

    def __init__(
        self,
        row_index: int,
        input_file: str,
        output_file: str,
        columns_to_keep: list[str],
        columns_to_validate: list[str],
        use_custom_regex: bool,
        custom_regex: str
    ):
        super().__init__()
        self.row_index = row_index
        self.input_file = input_file
        self.output_file = output_file
        self.columns_to_keep = columns_to_keep
        self.columns_to_validate = columns_to_validate
        self.use_custom_regex = use_custom_regex
        self.custom_regex = custom_regex
        self.stop_requested = False

    @pyqtSlot()
    def run(self):
        try:
            logging.info(f"Worker started for file: {self.input_file}")
            ext = os.path.splitext(self.input_file)[1].lower()
            if ext in ['.xlsx', '.xls']:
                self._process_excel()
            elif ext == '.csv':
                self._process_csv()
            else:
                raise ValueError(f"Unsupported file type: {ext}")

            msg = f"Completed -> {self.output_file}"
            logging.info(msg)
            self.finished.emit(self.row_index, msg)

        except Exception as e:
            logging.error(f"Worker error on {self.input_file}: {str(e)}", exc_info=True)
            self.error.emit(self.row_index, str(e))

    def stop(self):
        self.stop_requested = True

    def _process_excel(self):
        df = pd.read_excel(self.input_file)
        df_cleaned = self._clean_df(df)
        write_file(df_cleaned, self.output_file)
        self.progress.emit(self.row_index, 100)

    def _process_csv(self):
        # Count total rows (minus header)
        with open(self.input_file, 'r', encoding='utf-8-sig') as f:
            total_rows = sum(1 for _ in f) - 1
        if total_rows < 1:
            total_rows = 1

        chunksize = 20000
        rows_processed = 0
        first_chunk = True

        for chunk in chunked_csv_reader(self.input_file, chunksize=chunksize):
            if self.stop_requested:
                raise Exception("Stopped by user")

            logging.info(f"Processing chunk of size {len(chunk)} from {self.input_file}")
            cleaned = self._clean_df(chunk)
            write_file(cleaned, self.output_file, header=first_chunk, mode='w' if first_chunk else 'a')
            first_chunk = False

            rows_processed += len(chunk)
            progress_val = int(rows_processed / total_rows * 100)
            self.progress.emit(self.row_index, progress_val)

    def _clean_df(self, df: pd.DataFrame) -> pd.DataFrame:
        actual_cols = [c for c in self.columns_to_keep if c in df.columns]
        if not actual_cols:
            return pd.DataFrame()

        df = df[actual_cols]

        for col in self.columns_to_validate:
            if col in df.columns:
                df = df[df[col].apply(self._is_valid_wrapper)]
                if self.stop_requested:
                    raise Exception("Stopped by user")

        return df

    def _is_valid_wrapper(self, url: str) -> bool:
        if self.use_custom_regex and self.custom_regex:
            return regex_is_valid_url(url, self.custom_regex)
        else:
            return default_is_valid_url(url)

###############################################################################
# MAIN WINDOW
###############################################################################
class CleanWebsiteApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.setWindowTitle("Clean Website Data - Queue Concurrency + Directory + Remove + Open")

        # Data for concurrency
        self.tasks = []            # (row_index, input_file, output_file)
        self.max_concurrency = self.config.get("numThreads", 4)
        self.active_threads = []
        self.workers = []

        # Output directory (could be stored in config)
        self.output_dir = self.config.get("outputDir", "")

        # UI
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)

        self.create_top_section()
        self.create_middle_section()
        self.create_bottom_section()

        # Load config defaults
        self.apply_config_defaults()
        self.restore_window_geometry()

        self.resize(1100, 700)

        # Accept drops
        self.setAcceptDrops(True)

    ###########################################################################
    # UI CREATION
    ###########################################################################
    def create_top_section(self):
        group = QGroupBox("Input & Output Settings")
        layout = QGridLayout(group)

        # 1) Add files
        self.add_file_btn = QPushButton("Add File(s)")
        self.add_file_btn.clicked.connect(self.browse_input_files)
        layout.addWidget(self.add_file_btn, 0, 0)

        # 2) Remove selected
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self.remove_selected_files)
        layout.addWidget(self.remove_btn, 0, 1)

        # 3) Output Directory
        self.select_dir_btn = QPushButton("Select Output Directory")
        self.select_dir_btn.clicked.connect(self.select_output_directory)
        layout.addWidget(self.select_dir_btn, 0, 2)

        self.output_dir_label = QLabel("No directory selected")
        layout.addWidget(self.output_dir_label, 0, 3, 1, 2)

        # 4) Output base name
        layout.addWidget(QLabel("Output Base Name:"), 1, 0)
        self.base_name_edit = QLineEdit("cleaned_output")
        layout.addWidget(self.base_name_edit, 1, 1)

        # 5) Output format
        layout.addWidget(QLabel("Output Format:"), 1, 2)
        self.format_combo = QComboBox()
        self.format_combo.addItems(["csv", "xlsx"])
        layout.addWidget(self.format_combo, 1, 3)

        # 6) Threads
        layout.addWidget(QLabel("Number of Threads:"), 1, 4)
        self.thread_spin = QSpinBox()
        self.thread_spin.setRange(1, 16)
        layout.addWidget(self.thread_spin, 1, 5)

        # 7) Regex
        layout.addWidget(QLabel("Custom Regex:"), 2, 0)
        self.regex_edit = QLineEdit()
        layout.addWidget(self.regex_edit, 2, 1, 1, 2)

        # 8) Theme
        layout.addWidget(QLabel("Theme:"), 2, 3)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light", "Dark", "High Contrast"])
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        layout.addWidget(self.theme_combo, 2, 4)

        self.main_layout.addWidget(group)

    def create_middle_section(self):
        container = QHBoxLayout()

        # File table -> 4 columns: File, Progress, Status, Output File
        status_group = QGroupBox("File Status")
        sg_layout = QVBoxLayout(status_group)

        self.file_table = QTableWidget()
        self.file_table.setColumnCount(4)
        self.file_table.setHorizontalHeaderLabels(["File", "Progress", "Status", "Output File"])
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        sg_layout.addWidget(self.file_table)

        container.addWidget(status_group, 2)

        # Column selection
        col_group = QGroupBox("Column Selection")
        cg_layout = QVBoxLayout(col_group)

        col_layout = QHBoxLayout()
        self.keep_list = QListWidget()
        self.keep_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.validate_list = QListWidget()
        self.validate_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)

        col_layout.addWidget(QLabel("Columns to Keep:"))
        col_layout.addWidget(self.keep_list)
        col_layout.addWidget(QLabel("Validate as URL:"))
        col_layout.addWidget(self.validate_list)

        cg_layout.addLayout(col_layout)
        container.addWidget(col_group, 3)

        self.main_layout.addLayout(container)

    def create_bottom_section(self):
        # Preview
        preview_group = QGroupBox("Preview (first 5 rows)")
        pv_layout = QVBoxLayout(preview_group)
        self.preview_table = QTableWidget()
        pv_layout.addWidget(self.preview_table)
        self.main_layout.addWidget(preview_group)

        # Buttons row
        btn_layout = QHBoxLayout()

        self.preview_btn = QPushButton("Preview Selected")
        self.preview_btn.clicked.connect(self.preview_selected_file)

        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start_processing)

        self.stop_btn = QPushButton("Stop All")
        self.stop_btn.clicked.connect(self.stop_all)

        self.open_output_btn = QPushButton("Open Output File")
        self.open_output_btn.clicked.connect(self.open_selected_output_file)

        btn_layout.addWidget(self.preview_btn)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.open_output_btn)

        self.main_layout.addLayout(btn_layout)

    ###########################################################################
    # DRAG & DROP
    ###########################################################################
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            file_path = url.toLocalFile()
            if os.path.isfile(file_path):
                self.add_file_to_table(file_path)

    ###########################################################################
    # SELECT OUTPUT DIRECTORY
    ###########################################################################
    def select_output_directory(self):
        dir_ = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if dir_:
            self.output_dir = dir_
            self.output_dir_label.setText(dir_)

            # Save to config
            self.config["outputDir"] = dir_
            save_config(self.config)

    ###########################################################################
    # ADD / REMOVE FILES
    ###########################################################################
    def browse_input_files(self):
        dialog = QFileDialog(self, "Select Input Files")
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setNameFilters(["CSV Files (*.csv)", "Excel Files (*.xlsx *.xls)"])
        if dialog.exec():
            selected = dialog.selectedFiles()
            for f in selected:
                self.add_file_to_table(f)

    def add_file_to_table(self, file_path: str):
        row = self.file_table.rowCount()
        self.file_table.insertRow(row)
        self.file_table.setItem(row, 0, QTableWidgetItem(file_path))
        self.file_table.setItem(row, 1, QTableWidgetItem("0%"))
        self.file_table.setItem(row, 2, QTableWidgetItem("Pending"))
        self.file_table.setItem(row, 3, QTableWidgetItem(""))  # Output file (empty for now)

    def remove_selected_files(self):
        """
        Remove selected row(s) from the table.
        """
        rows = self.file_table.selectionModel().selectedRows()
        # We should remove from bottom to top to avoid reindexing issues
        for r in sorted(rows, key=lambda x: x.row(), reverse=True):
            self.file_table.removeRow(r.row())

    ###########################################################################
    # PREVIEW
    ###########################################################################
    def preview_selected_file(self):
        row = self.file_table.currentRow()
        if row < 0:
            return
        item = self.file_table.item(row, 0)
        if not item:
            return
        file_path = item.text().strip()
        self.load_preview(file_path)

    def load_preview(self, file_path: str):
        try:
            df = read_preview_df(file_path, nrows=5)
            self.populate_preview_table(df)
            self.fuzzy_detect_columns(df.columns)
        except Exception as e:
            QMessageBox.warning(self, "Preview Error", str(e))
            logging.error(f"Preview error: {e}", exc_info=True)

    def populate_preview_table(self, df: pd.DataFrame):
        self.preview_table.clear()
        self.preview_table.setRowCount(len(df))
        self.preview_table.setColumnCount(len(df.columns))
        self.preview_table.setHorizontalHeaderLabels(df.columns)

        for r in range(len(df)):
            for c in range(len(df.columns)):
                val = df.iat[r, c]
                self.preview_table.setItem(r, c, QTableWidgetItem(str(val) if pd.notna(val) else ""))

    ###########################################################################
    # FUZZY COLUMN DETECTION
    ###########################################################################
    def fuzzy_detect_columns(self, columns):
        self.keep_list.clear()
        self.validate_list.clear()

        col_list = list(columns)

        title_keywords = ["title", "titel", "titulo", "заголовок", "titolo"]
        web_keywords = ["website", "web", "url", "site", "homepage"]

        for col in col_list:
            item_keep = QListWidgetItem(col)
            item_keep.setFlags(item_keep.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item_keep.setCheckState(Qt.CheckState.Unchecked)

            item_val = QListWidgetItem(col)
            item_val.setFlags(item_val.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item_val.setCheckState(Qt.CheckState.Unchecked)

            if process is not None:
                # fuzzy approach
                best_title = process.extractOne(col.lower(), title_keywords, scorer=fuzz.partial_ratio)
                if best_title and best_title[1] > 70:
                    item_keep.setCheckState(Qt.CheckState.Checked)

                best_web = process.extractOne(col.lower(), web_keywords, scorer=fuzz.partial_ratio)
                if best_web and best_web[1] > 70:
                    item_keep.setCheckState(Qt.CheckState.Checked)
                    item_val.setCheckState(Qt.CheckState.Checked)
            else:
                # fallback substring check
                low = col.lower()
                if "title" in low or "web" in low or "site" in low:
                    item_keep.setCheckState(Qt.CheckState.Checked)
                if "web" in low or "url" in low or "site" in low:
                    item_val.setCheckState(Qt.CheckState.Checked)

            self.keep_list.addItem(item_keep)
            self.validate_list.addItem(item_val)

    ###########################################################################
    # START & STOP PROCESSING
    ###########################################################################
    def start_processing(self):
        row_count = self.file_table.rowCount()
        if row_count == 0:
            QMessageBox.information(self, "No Files", "Please add files first.")
            return

        if not self.output_dir:
            QMessageBox.warning(self, "No Output Directory", "Please select an output directory.")
            return

        base_name = self.base_name_edit.text().strip()
        if not base_name:
            base_name = "cleaned_output"
        out_format = self.format_combo.currentText()  # 'csv' or 'xlsx'
        custom_regex = self.regex_edit.text().strip()

        # Save config
        self.config["lastRegex"] = custom_regex
        self.config["lastOutputBaseName"] = base_name
        self.config["numThreads"] = self.thread_spin.value()
        self.config["outputDir"] = self.output_dir
        save_config(self.config)

        # Collect columns
        columns_to_keep = self.get_checked_items(self.keep_list)
        columns_to_validate = self.get_checked_items(self.validate_list)

        # Reset table statuses
        for r in range(row_count):
            self.file_table.setItem(r, 1, QTableWidgetItem("0%"))
            self.file_table.setItem(r, 2, QTableWidgetItem("Pending"))
            self.file_table.setItem(r, 3, QTableWidgetItem(""))

        # Build queue
        self.tasks.clear()
        for r in range(row_count):
            file_item = self.file_table.item(r, 0)
            if not file_item:
                continue
            file_in = file_item.text().strip()
            if not os.path.isfile(file_in):
                self.file_table.setItem(r, 2, QTableWidgetItem("File Not Found"))
                continue

            # Construct output file inside output_dir
            out_file = f"{base_name}_{os.path.splitext(os.path.basename(file_in))[0]}"
            ext = ".csv" if out_format == "csv" else ".xlsx"
            if not out_file.lower().endswith(ext):
                out_file += ext
            full_out = os.path.join(self.output_dir, out_file)

            self.tasks.append((r, file_in, full_out))

        self.max_concurrency = self.thread_spin.value()
        self.active_threads.clear()
        self.workers.clear()

        logging.info(f"Starting concurrency with up to {self.max_concurrency} threads.")
        self.schedule_next_tasks()

    def schedule_next_tasks(self):
        while len(self.active_threads) < self.max_concurrency and len(self.tasks) > 0:
            r, in_file, out_file = self.tasks.pop(0)

            self.file_table.setItem(r, 2, QTableWidgetItem("Processing"))
            self.file_table.setItem(r, 3, QTableWidgetItem(out_file))

            columns_to_keep = self.get_checked_items(self.keep_list)
            columns_to_validate = self.get_checked_items(self.validate_list)
            custom_regex = self.regex_edit.text().strip()
            use_custom_regex = bool(custom_regex)

            worker = CleanDataWorker(
                row_index=r,
                input_file=in_file,
                output_file=out_file,
                columns_to_keep=columns_to_keep,
                columns_to_validate=columns_to_validate,
                use_custom_regex=use_custom_regex,
                custom_regex=custom_regex
            )
            thread = QThread()
            worker.moveToThread(thread)

            # Connect signals
            thread.started.connect(worker.run)
            worker.progress.connect(self.on_worker_progress)
            worker.finished.connect(self.on_worker_finished)
            worker.error.connect(self.on_worker_error)

            # Cleanup
            worker.finished.connect(lambda row, msg, t=thread: self.on_thread_complete(t))
            worker.error.connect(lambda row, msg, t=thread: self.on_thread_complete(t))
            worker.finished.connect(thread.quit)
            worker.error.connect(thread.quit)

            self.active_threads.append(thread)
            self.workers.append(worker)
            thread.start()

    def stop_all(self):
        """
        Stop all active workers & clear remaining tasks.
        """
        logging.info("Stop requested for all workers.")
        self.tasks.clear()  # no more tasks
        for w in self.workers:
            w.stop()

    ###########################################################################
    # THREAD SIGNALS
    ###########################################################################
    @pyqtSlot(int, int)
    def on_worker_progress(self, row_index: int, val: int):
        self.file_table.setItem(row_index, 1, QTableWidgetItem(f"{val}%"))

    @pyqtSlot(int, str)
    def on_worker_finished(self, row_index: int, message: str):
        self.file_table.setItem(row_index, 1, QTableWidgetItem("100%"))
        self.file_table.setItem(row_index, 2, QTableWidgetItem("Completed"))
        logging.info(f"Worker finished: Row {row_index}, {message}")

    @pyqtSlot(int, str)
    def on_worker_error(self, row_index: int, error_msg: str):
        self.file_table.setItem(row_index, 2, QTableWidgetItem(f"Error: {error_msg}"))
        logging.error(f"Worker error row {row_index}: {error_msg}")

    def on_thread_complete(self, thread: QThread):
        if thread in self.active_threads:
            self.active_threads.remove(thread)
        self.schedule_next_tasks()

    ###########################################################################
    # OPEN OUTPUT FILE
    ###########################################################################
    def open_selected_output_file(self):
        """
        Opens the output file of the currently selected row, if any.
        """
        row = self.file_table.currentRow()
        if row < 0:
            return
        out_item = self.file_table.item(row, 3)
        if not out_item:
            return

        output_path = out_item.text().strip()
        if not output_path or not os.path.isfile(output_path):
            QMessageBox.warning(self, "Open Output", "Output file not found or doesn't exist yet.")
            return

        self.open_file_in_system_default_app(output_path)

    def open_file_in_system_default_app(self, file_path: str):
        """
        Cross-platform 'open file with default app' approach.
        """
        if sys.platform.startswith("win"):
            os.startfile(file_path)
        elif sys.platform == "darwin":
            subprocess.call(["open", file_path])
        else:
            subprocess.call(["xdg-open", file_path])

    ###########################################################################
    # HELPER METHODS
    ###########################################################################
    def get_checked_items(self, list_widget: QListWidget) -> list[str]:
        result = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                result.append(item.text())
        return result

    def apply_config_defaults(self):
        rx = self.config.get("lastRegex", "")
        self.regex_edit.setText(rx)
        bn = self.config.get("lastOutputBaseName", "cleaned_output")
        self.base_name_edit.setText(bn)
        threads = self.config.get("numThreads", 4)
        self.thread_spin.setValue(threads)

        self.output_dir = self.config.get("outputDir", "")
        if self.output_dir:
            self.output_dir_label.setText(self.output_dir)

        theme = self.config.get("theme", "Light")
        idx = self.theme_combo.findText(theme)
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)

    def restore_window_geometry(self):
        geom = self.config.get("windowGeometry", None)
        if geom and isinstance(geom, list) and len(geom) == 4:
            self.setGeometry(QRect(*geom))

    def closeEvent(self, event):
        g = [self.x(), self.y(), self.width(), self.height()]
        self.config["windowGeometry"] = g

        style = self.styleSheet()
        if "background-color: #2f2f2f" in style:
            self.config["theme"] = "Dark"
        elif "background-color: black" in style:
            self.config["theme"] = "High Contrast"
        else:
            self.config["theme"] = "Light"

        save_config(self.config)
        super().closeEvent(event)

    ###########################################################################
    # THEME SWITCHING
    ###########################################################################
    def on_theme_changed(self):
        theme = self.theme_combo.currentText()
        if theme == "Light":
            self.setStyleSheet("")
        elif theme == "Dark":
            self.apply_dark_theme()
        elif theme == "High Contrast":
            self.apply_high_contrast_theme()

    def apply_dark_theme(self):
        dark_stylesheet = """
            QMainWindow, QWidget {
                background-color: #2f2f2f;
                color: #dddddd;
            }
            QLineEdit, QTableWidget, QListWidget, QComboBox, QSpinBox {
                background-color: #3f3f3f;
                color: #ffffff;
            }
            QPushButton {
                background-color: #4f4f4f;
                color: #ffffff;
            }
            QCheckBox, QRadioButton, QLabel, QGroupBox {
                color: #ffffff;
            }
            QProgressBar {
                background-color: #3f3f3f;
                color: #ffffff;
            }
        """
        self.setStyleSheet(dark_stylesheet)

    def apply_high_contrast_theme(self):
        hc_stylesheet = """
            QMainWindow, QWidget {
                background-color: black;
                color: yellow;
            }
            QLineEdit, QTableWidget, QListWidget, QComboBox, QSpinBox {
                background-color: black;
                color: yellow;
            }
            QPushButton {
                background-color: white;
                color: black;
                font-weight: bold;
            }
            QCheckBox, QRadioButton, QLabel, QGroupBox {
                color: yellow;
                font-weight: bold;
            }
            QProgressBar {
                background-color: white;
                color: black;
            }
        """
        self.setStyleSheet(hc_stylesheet)

###############################################################################
# MAIN
###############################################################################
def main():
    app = QApplication(sys.argv)
    window = CleanWebsiteApp()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
