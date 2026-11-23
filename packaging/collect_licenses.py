from importlib.metadata import distribution
from pathlib import Path
import shutil
import sys

output = Path(sys.argv[1]) / "licenses"
output.mkdir(exist_ok=True)
for name in ("PySide6-Essentials", "shiboken6", "openpyxl", "et_xmlfile", "lxml"):
    package = distribution(name)
    target = output / name
    target.mkdir(exist_ok=True)
    for relative in package.files or []:
        filename = relative.name.casefold()
        if ("license" in filename or "licence" in filename or
                filename.startswith(("authors", "copying", "notice", "lgpl", "gpl", "fdl", "gfdl", "qt-attribution"))):
            path = package.locate_file(relative)
            if path.is_file():
                shutil.copyfile(path, target / relative.name)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    shutil.copyfile(python_license, output / "Python-LICENSE.txt")
shutil.copyfile(Path(__file__).with_name("third_party.txt"), output / "Сведения о компонентах.txt")
for path in Path(__file__).with_name("licenses").glob("*.txt"):
    shutil.copyfile(path, output / path.name)
