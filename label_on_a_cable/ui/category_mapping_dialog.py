"""Category mapping dialog.

Lets the user:
1. Map each QGIS layer to a LOC category
2. Map each LOC category field to a QGIS layer attribute
3. Save / load named mapping presets
"""

from typing import Dict, List, Optional

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsProject, QgsVectorLayer, QgsWkbTypes

from ..core.tasks import FetchCategoriesTask
from ..models.category import Category
from ..models.mapping import (
    FieldMapping,
    LayerMapping,
    MappingPreset,
    delete_preset,
    load_all_presets,
    save_preset,
)
from ..services.api_client import ApiClient


class CategoryMappingDialog(QDialog):
    """Modal dialog for layer → category + field mapping.

    Emits ``mapping_accepted`` with the list of LayerMappings on OK.
    """

    mapping_accepted = pyqtSignal(list)  # List[LayerMapping]

    def __init__(
        self,
        api_client: ApiClient,
        location_id: str,
        current_mappings: Optional[List[LayerMapping]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Category Mapping")
        self.setMinimumSize(560, 400)

        self.api = api_client
        self.location_id = location_id
        self._categories: List[Category] = []
        self._task: Optional[FetchCategoriesTask] = None

        # Restore previous mappings if provided
        self._initial_mappings: Dict[str, LayerMapping] = {}
        for lm in (current_mappings or []):
            self._initial_mappings[lm.layer_id] = lm

        # Built dynamically after categories load
        self._layer_rows: List[_LayerRow] = []

        self._build_ui()
        self._fetch_categories()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)

        # Description
        desc = QLabel(
            "Map each QGIS layer to a LOC category and configure "
            "field mappings."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: gray; font-size: 11px; margin-bottom: 4px;")
        root.addWidget(desc)

        # Preset bar
        preset_bar = QHBoxLayout()
        preset_bar.addWidget(QLabel("Preset:"))
        self._preset_combo = QComboBox()
        self._preset_combo.setMinimumWidth(160)
        preset_bar.addWidget(self._preset_combo)

        btn_load = QPushButton("Load")
        btn_load.clicked.connect(self._load_preset)
        preset_bar.addWidget(btn_load)

        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self._save_preset)
        preset_bar.addWidget(btn_save)

        btn_del = QPushButton("Delete")
        btn_del.clicked.connect(self._delete_preset)
        preset_bar.addWidget(btn_del)

        preset_bar.addStretch()
        root.addLayout(preset_bar)

        # "Create in LOC" bulk toggle bar
        export_bar = QHBoxLayout()
        export_bar.addWidget(QLabel("Create in LOC:"))
        btn_select_all = QPushButton("Select All")
        btn_select_all.clicked.connect(self._select_all_export)
        export_bar.addWidget(btn_select_all)
        btn_deselect_all = QPushButton("Deselect All")
        btn_deselect_all.clicked.connect(self._deselect_all_export)
        export_bar.addWidget(btn_deselect_all)
        export_bar.addStretch()
        root.addLayout(export_bar)

        # Status label
        self._status = QLabel("Loading categories...")
        self._status.setVisible(True)
        root.addWidget(self._status)

        # Scrollable area for layer rows
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._scroll_content)
        root.addWidget(self._scroll)

        # OK / Cancel
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(False)
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self._on_cancel)
        root.addWidget(self._buttons)

        # Populate preset combo from saved presets
        self._refresh_preset_combo()

    def _on_cancel(self):
        """Cancel any running task and close."""
        if self._task is not None:
            self._task.taskCompleted.disconnect(self._on_categories_loaded)
            self._task.taskTerminated.disconnect(self._on_categories_loaded)
            self._task = None
        self.reject()

    # ------------------------------------------------------------------
    # Category fetch
    # ------------------------------------------------------------------

    def _fetch_categories(self):
        self._task = FetchCategoriesTask(self.api, self.location_id)
        self._task.taskCompleted.connect(self._on_categories_loaded)
        self._task.taskTerminated.connect(self._on_categories_loaded)
        QgsApplication.taskManager().addTask(self._task)

    def _on_categories_loaded(self):
        task = self._task
        self._task = None
        if task is None:
            return

        if task.error:
            self._status.setText(task.error)
            self._status.setStyleSheet("color: red;")
            return

        self._categories = task.categories

        # Convert downloaded icon bytes → QIcon
        self._cat_icons: Dict[str, QIcon] = {}
        for cat_id, data in task.icon_data.items():
            pm = QPixmap()
            if pm.loadFromData(data):
                self._cat_icons[cat_id] = QIcon(
                    pm.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )

        self._status.setVisible(False)
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(True)
        self._build_layer_rows()

    # ------------------------------------------------------------------
    # Layer rows
    # ------------------------------------------------------------------

    def _build_layer_rows(self):
        # Clear previous rows
        for row in self._layer_rows:
            row.group.deleteLater()
        self._layer_rows.clear()

        project = QgsProject.instance()
        # Fetch each layer by ID to avoid SIP wrapper reuse when iterating
        # mapLayers().values() directly.
        for layer_id in list(project.mapLayers().keys()):
            layer = project.mapLayer(layer_id)
            if not isinstance(layer, QgsVectorLayer):
                continue
            # Only point and line layers are relevant for LOC export
            if layer.geometryType() not in (
                QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry,
            ):
                continue
            row = _LayerRow(layer, self._categories, self._cat_icons,
                           self._scroll_content)

            # Restore previous mapping if available
            prev = self._initial_mappings.get(layer.id())
            if prev:
                row.restore(prev)

            self._scroll_layout.addWidget(row.group)
            self._layer_rows.append(row)

    # ------------------------------------------------------------------
    # Accept → emit mappings
    # ------------------------------------------------------------------

    def _on_accept(self):
        mappings = [row.to_mapping() for row in self._layer_rows
                    if row.is_mapped()]
        self.mapping_accepted.emit(mappings)
        self.accept()

    # ------------------------------------------------------------------
    # Presets
    # ------------------------------------------------------------------

    def _refresh_preset_combo(self):
        self._preset_combo.clear()
        for name in sorted(load_all_presets()):
            self._preset_combo.addItem(name)

    def _save_preset(self):
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:",
            text=self._preset_combo.currentText(),
        )
        if not ok or not name.strip():
            return
        mappings = [row.to_mapping() for row in self._layer_rows
                    if row.is_mapped()]
        preset = MappingPreset(name=name.strip(), layer_mappings=mappings)
        save_preset(preset)
        self._refresh_preset_combo()
        self._preset_combo.setCurrentText(name.strip())

    def _load_preset(self):
        name = self._preset_combo.currentText()
        if not name:
            return
        presets = load_all_presets()
        preset = presets.get(name)
        if not preset:
            return
        lookup_by_id = {lm.layer_id: lm for lm in preset.layer_mappings}
        lookup_by_name = {lm.layer_name: lm for lm in preset.layer_mappings}
        for row in self._layer_rows:
            prev = lookup_by_id.get(row.layer_id)
            if not prev:
                prev = lookup_by_name.get(row.layer_name)
            if prev:
                row.restore(prev)
            else:
                row.reset()

    # ------------------------------------------------------------------
    # Bulk export toggle
    # ------------------------------------------------------------------

    def _select_all_export(self):
        for row in self._layer_rows:
            row._export_cb.setChecked(True)

    def _deselect_all_export(self):
        for row in self._layer_rows:
            row._export_cb.setChecked(False)

    def _delete_preset(self):
        name = self._preset_combo.currentText()
        if not name:
            return
        if QMessageBox.question(
            self, "Delete Preset",
            f'Delete preset "{name}"?',
        ) != QMessageBox.Yes:
            return
        delete_preset(name)
        self._refresh_preset_combo()


# ======================================================================
# Internal helper: one row per QGIS layer
# ======================================================================

class _LayerRow:
    """UI for mapping a single QGIS layer → LOC category + fields."""

    def __init__(self, layer: QgsVectorLayer, categories: List[Category],
                 icons: Dict[str, QIcon], parent: QWidget):
        self.layer_id = layer.id()
        self.layer_name = layer.name()
        # Snapshot field names into a plain Python list immediately.
        # Exclude auxiliary_storage_ fields (QGIS labelling internals).
        self._qgis_fields = [
            name for name in layer.fields().names()
            if not name.startswith("auxiliary_storage_")
        ]

        # Keep single LOC categories for the stop category combo (line layers)
        self._single_categories = [c for c in categories if c.is_single]

        # Filter categories by geometry type:
        #   Point layers → single LOC categories only
        #   LineString layers → dual LOC categories only
        geom_type = layer.geometryType()
        if geom_type == QgsWkbTypes.PointGeometry:
            self._categories = list(self._single_categories)
        elif geom_type == QgsWkbTypes.LineGeometry:
            self._categories = [c for c in categories if c.is_dual]
        else:
            self._categories = list(categories)

        self._is_point_layer = (geom_type == QgsWkbTypes.PointGeometry)
        self._is_line_layer = (geom_type == QgsWkbTypes.LineGeometry)

        self.group = QGroupBox(parent=parent)
        layout = QVBoxLayout(self.group)

        # Header row: bold layer name + "Create in LOC" checkbox
        header_row = QHBoxLayout()
        name_label = QLabel(f"<b>{self.layer_name}</b>")
        header_row.addWidget(name_label)
        header_row.addStretch()
        self._export_cb = QCheckBox("Create in LOC")
        self._export_cb.setChecked(True)
        self._export_cb.setToolTip(
            "When unchecked, this layer will not be included in the "
            "push to LOC. Its mapping is preserved but inactive."
        )
        header_row.addWidget(self._export_cb)
        layout.addLayout(header_row)

        # Container for all mapping controls — collapsed when export is off
        self._detail_widget = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_widget)
        self._detail_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._detail_widget)

        self._export_cb.toggled.connect(self._on_export_toggled)

        # Category combo
        cat_row = QHBoxLayout()
        cat_row.addWidget(QLabel("LOC Category:"))
        self._cat_combo = QComboBox()
        self._cat_combo.addItem("(unmapped)", "")
        for cat in self._categories:
            icon = icons.get(cat.category_id)
            if icon:
                self._cat_combo.addItem(icon, cat.name, cat.category_id)
            else:
                self._cat_combo.addItem(cat.name, cat.category_id)
        self._cat_combo.currentIndexChanged.connect(self._on_cat_changed)
        cat_row.addWidget(self._cat_combo)
        self._detail_layout.addLayout(cat_row)

        # Include in route generation toggle
        if self._is_point_layer:
            include_label = "Include as stops in routes"
        else:
            include_label = "Generate routes for this layer"
        self._include_cb = QCheckBox(include_label)
        self._include_cb.setChecked(True)
        self._include_cb.setToolTip(
            "When unchecked, this layer is excluded from route generation."
        )
        self._detail_layout.addWidget(self._include_cb)

        # Default stop type combo (point layers only)
        self._stop_type_combo = None
        if self._is_point_layer:
            st_row = QHBoxLayout()
            st_row.addWidget(QLabel("Default stop type:"))
            self._stop_type_combo = QComboBox()
            self._stop_type_combo.addItem("Passthrough", "passthrough")
            self._stop_type_combo.addItem("Ingress / Egress", "ingress_egress")
            self._stop_type_combo.setToolTip(
                "Passthrough: single label per stop.\n"
                "Ingress/Egress: paired IN/OUT labels at each stop."
            )
            st_row.addWidget(self._stop_type_combo)
            st_row.addStretch()
            self._detail_layout.addLayout(st_row)

        # Field mappings (built dynamically when category changes)
        self._fields_widget = QWidget()
        self._fields_layout = QFormLayout(self._fields_widget)
        self._detail_layout.addWidget(self._fields_widget)

        self._field_combos: List[tuple] = []  # (loc_field_name, QComboBox)

        # Stop category combo + stop field mappings (line layers only)
        self._stop_cat_combo = None
        self._stop_field_combos: List[tuple] = []
        if self._is_line_layer:
            sc_row = QHBoxLayout()
            sc_row.addWidget(QLabel("Stop Category:"))
            self._stop_cat_combo = QComboBox()
            self._stop_cat_combo.addItem("(select stop category)", "")
            for cat in self._single_categories:
                icon = icons.get(cat.category_id)
                if icon:
                    self._stop_cat_combo.addItem(icon, cat.name, cat.category_id)
                else:
                    self._stop_cat_combo.addItem(cat.name, cat.category_id)
            self._stop_cat_combo.setToolTip(
                "The LOC category used for stop labels on this cable route.\n"
                "Stops are derived from point features along the line."
            )
            self._stop_cat_combo.currentIndexChanged.connect(
                self._on_stop_cat_changed
            )
            sc_row.addWidget(self._stop_cat_combo)
            self._detail_layout.addLayout(sc_row)

            # Indented stop field mappings (built when stop category changes)
            self._stop_fields_widget = QWidget()
            stop_outer = QHBoxLayout(self._stop_fields_widget)
            stop_outer.setContentsMargins(20, 0, 0, 0)  # indent
            self._stop_fields_layout = QFormLayout()
            stop_outer.addLayout(self._stop_fields_layout)
            self._detail_layout.addWidget(self._stop_fields_widget)

    def _on_export_toggled(self, checked: bool):
        """Show or collapse the mapping detail section."""
        self._detail_widget.setVisible(checked)

    def _on_cat_changed(self, _index):
        cat = self._selected_category()
        self._rebuild_field_combos(cat)

    def _on_stop_cat_changed(self, _index):
        cat = self._selected_stop_category()
        self._rebuild_stop_field_combos(cat)

    def _selected_category(self) -> Optional[Category]:
        cat_id = self._cat_combo.currentData()
        if not cat_id:
            return None
        for c in self._categories:
            if c.category_id == cat_id:
                return c
        return None

    # Essential LOC fields that must always appear for mapping,
    # even if the category definition doesn't include them.
    _ESSENTIAL_SINGLE = ["Unique Asset Identifier", "Actual Asset Name"]
    _ESSENTIAL_DUAL = ["Route ID"]

    def _rebuild_field_combos(self, cat: Optional[Category]):
        # Clear old combos
        for i in reversed(range(self._fields_layout.count())):
            w = self._fields_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        self._field_combos.clear()

        if cat is None:
            return

        all_fields = cat.fields + cat.destination_fields

        # Determine which essential fields to inject
        if cat.is_single:
            essentials = self._ESSENTIAL_SINGLE
        else:
            essentials = self._ESSENTIAL_DUAL

        # Collect existing field names (case-insensitive) to avoid duplicates
        existing = {cf.name.lower() for cf in all_fields}

        # Show essential fields first (bold label to distinguish them)
        for ename in essentials:
            if ename.lower() not in existing:
                self._add_field_combo(f"<b>{ename}</b> (required):", ename)
            # If the field IS in the category, it will appear in its
            # normal position below — no special injection needed.

        for cf in all_fields:
            label = f"{cf.name}:"
            # Mark essential fields that came from the category definition
            if cf.name.lower() in {e.lower() for e in essentials}:
                label = f"<b>{cf.name}</b> (required):"
            self._add_field_combo(label, cf.name)

    def _add_field_combo(self, label: str, loc_field_name: str):
        """Add one field mapping row (LOC field → QGIS attribute combo)."""
        combo = QComboBox()
        combo.addItem("(unmapped)", "")
        for qf in self._qgis_fields:
            combo.addItem(qf, qf)
        # Use a QLabel to support HTML bold formatting
        lbl = QLabel(label)
        if "(required)" in label:
            lbl.setToolTip(
                "This field must be mapped for push to work correctly."
            )
        self._fields_layout.addRow(lbl, combo)
        self._field_combos.append((loc_field_name, combo))

    # ------------------------------------------------------------------
    # Stop category field mapping (line layers only)
    # ------------------------------------------------------------------

    def _rebuild_stop_field_combos(self, cat: Optional[Category]):
        """Rebuild field combos for the stop category."""
        # Clear old combos
        for i in reversed(range(self._stop_fields_layout.count())):
            w = self._stop_fields_layout.itemAt(i).widget()
            if w:
                w.deleteLater()
        self._stop_field_combos.clear()

        if cat is None:
            return

        all_fields = cat.fields
        # Stop essentials: only Actual Asset Name — Unique Asset Identifier
        # is always resolved from the point feature automatically.
        essentials = ["Actual Asset Name"]
        existing = {cf.name.lower() for cf in all_fields}

        # Inject essential fields if missing from category definition
        for ename in essentials:
            if ename.lower() not in existing:
                self._add_stop_field_combo(
                    f"<b>{ename}</b> (required):", ename
                )

        for cf in all_fields:
            # Skip Unique Asset Identifier — filled automatically
            if cf.name.lower() == "unique asset identifier":
                continue
            label = f"{cf.name}:"
            if cf.name.lower() in {e.lower() for e in essentials}:
                label = f"<b>{cf.name}</b> (required):"
            self._add_stop_field_combo(label, cf.name)

    def _add_stop_field_combo(self, label: str, loc_field_name: str):
        """Add one stop field mapping row."""
        combo = QComboBox()
        combo.addItem("(unmapped)", "")
        for qf in self._qgis_fields:
            combo.addItem(qf, qf)
        lbl = QLabel(label)
        if "(required)" in label:
            lbl.setToolTip(
                "This field must be mapped for push to work correctly."
            )
        self._stop_fields_layout.addRow(lbl, combo)
        self._stop_field_combos.append((loc_field_name, combo))

    def is_mapped(self) -> bool:
        return bool(self._cat_combo.currentData())

    def _selected_stop_category(self) -> Optional[Category]:
        if self._stop_cat_combo is None:
            return None
        cat_id = self._stop_cat_combo.currentData()
        if not cat_id:
            return None
        for c in self._single_categories:
            if c.category_id == cat_id:
                return c
        return None

    def to_mapping(self) -> LayerMapping:
        cat = self._selected_category()
        stop_type = "passthrough"
        if self._stop_type_combo is not None:
            stop_type = self._stop_type_combo.currentData() or "passthrough"
        stop_cat = self._selected_stop_category()
        return LayerMapping(
            layer_id=self.layer_id,
            layer_name=self.layer_name,
            category_id=cat.category_id if cat else "",
            category_name=cat.name if cat else "",
            field_mappings=[
                FieldMapping(loc_field=name, qgis_field=combo.currentData() or "")
                for name, combo in self._field_combos
            ],
            default_stop_type=stop_type,
            stop_category_id=stop_cat.category_id if stop_cat else "",
            stop_category_name=stop_cat.name if stop_cat else "",
            stop_field_mappings=[
                FieldMapping(loc_field=name, qgis_field=combo.currentData() or "")
                for name, combo in self._stop_field_combos
            ],
            include_in_routes=self._include_cb.isChecked(),
            enabled_for_export=self._export_cb.isChecked(),
        )

    def restore(self, lm: LayerMapping):
        """Restore a previous mapping into this row's UI."""
        # Set category combo
        idx = self._cat_combo.findData(lm.category_id)
        if idx >= 0:
            self._cat_combo.setCurrentIndex(idx)

        # Set field combos
        field_lookup = {fm.loc_field: fm.qgis_field for fm in lm.field_mappings}
        for loc_name, combo in self._field_combos:
            qgis_name = field_lookup.get(loc_name, "")
            fidx = combo.findData(qgis_name)
            if fidx >= 0:
                combo.setCurrentIndex(fidx)

        # Set stop category combo (line layers) — triggers _rebuild_stop_field_combos
        if self._stop_cat_combo is not None and lm.stop_category_id:
            sc_idx = self._stop_cat_combo.findData(lm.stop_category_id)
            if sc_idx >= 0:
                self._stop_cat_combo.setCurrentIndex(sc_idx)

            # Restore stop field combos
            stop_lookup = {
                fm.loc_field: fm.qgis_field for fm in lm.stop_field_mappings
            }
            for loc_name, combo in self._stop_field_combos:
                qgis_name = stop_lookup.get(loc_name, "")
                fidx = combo.findData(qgis_name)
                if fidx >= 0:
                    combo.setCurrentIndex(fidx)

        # Set stop type combo
        if self._stop_type_combo is not None:
            st_idx = self._stop_type_combo.findData(lm.default_stop_type)
            if st_idx >= 0:
                self._stop_type_combo.setCurrentIndex(st_idx)

        # Set include in routes checkbox
        self._include_cb.setChecked(lm.include_in_routes)

        # Set export toggle (triggers collapse/expand)
        self._export_cb.setChecked(lm.enabled_for_export)

    def reset(self):
        """Reset this row to unmapped."""
        self._cat_combo.setCurrentIndex(0)
        self._include_cb.setChecked(True)
        self._export_cb.setChecked(True)
