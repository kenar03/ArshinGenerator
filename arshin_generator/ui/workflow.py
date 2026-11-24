from getpass import getuser
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QRadioButton, QWidget

from ..laboratory import DEFAULT_FIELDS, validate_defaults
from .common import Form, button, label


def laboratory_settings(parent, storage):
    laboratory = storage.laboratory()
    xml_output = storage.xml_output()
    form = Form(parent, "Настройки", size=(665, 440), save="Сохранить настройки")
    fields = form.editor.form
    fields.addRow(label("Реквизиты лаборатории", "heading"))
    fields.addRow(label("Программа использует эти сведения, когда соответствующее поле в Excel пустое."))
    for key, title, kind in DEFAULT_FIELDS:
        control = form.field(key, title, kind, laboratory.get(key))
        if key == "sign_cipher":
            control.setPlaceholderText("Например, ВЦЯ")
    initial_fields = form.values()

    fields.addRow(label("Сохранение XML", "heading"))
    manual = QRadioButton("Каждый раз выбирать место сохранения", form.editor)
    automatic = QRadioButton("Автоматически сохранять в папку", form.editor)
    fields.addRow(manual)
    fields.addRow(automatic)
    destination = QWidget(form.editor)
    row = QHBoxLayout(destination)
    row.setContentsMargins(0, 0, 0, 0)
    folder = QLineEdit(xml_output[1])
    folder.setReadOnly(True)
    folder.setAccessibleName("Папка автоматического сохранения")
    folder.setPlaceholderText("Выберите папку")
    row.addWidget(folder, 1)

    def browse():
        path = QFileDialog.getExistingDirectory(form, "Папка для XML", folder.text() or str(parent.output_folder()))
        if path:
            folder.setText(str(Path(path)))

    row.addWidget(button("Обзор…", browse))
    fields.addRow(destination)
    (automatic if xml_output[0] else manual).setChecked(True)
    destination.setEnabled(automatic.isChecked())
    automatic.toggled.connect(destination.setEnabled)

    def validate(values):
        if values != initial_fields:
            validate_defaults(values)
        if automatic.isChecked() and not Path(folder.text()).is_absolute():
            raise ValueError("Выберите папку автоматического сохранения XML.")

    form.validate = validate
    if form.exec():
        output = automatic.isChecked(), folder.text()
        if output != xml_output:
            storage.save_xml_output(*output)
        values = form.values()
        if values != initial_fields:
            storage.save_laboratory({**laboratory, **values}, getuser())
            return True
    return False
