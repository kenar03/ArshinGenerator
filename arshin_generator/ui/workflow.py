from getpass import getuser

from ..laboratory import DEFAULT_FIELDS, validate_defaults
from .common import Form


def laboratory_settings(parent, storage):
    settings = storage.laboratory()
    form = Form(parent, "Реквизиты лаборатории",
                "Указывайте постоянные сведения. Программа использует их, когда соответствующее поле в Excel пустое.",
                size=(665, 370), save="Сохранить настройки")
    for key, title, kind in DEFAULT_FIELDS:
        control = form.field(key, title, kind, settings.get(key))
        if key == "sign_cipher":
            control.setPlaceholderText("Например, ВЦЯ")
    form.validate = validate_defaults
    if form.exec():
        values = {**settings, **form.values()}
        if values != settings:
            storage.save_laboratory(values, getuser())
            return True
    return False
