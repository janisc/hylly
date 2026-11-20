"""Hylly: a self-hosted library for the e-books and comics you own.

Copyright (C) 2026 janisc and the Hylly contributors

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
details.

You should have received a copy of the GNU Affero General Public License along with this program. If not, see
<https://www.gnu.org/licenses/>.
"""

__version__ = "0.1.0"
# Where Hylly's source is: the project's page. The Source link a running Hylly offers its readers goes there unless
# [server] source_url names a changed version's own (settings.py), and the sites Hylly asks are told it (USER_AGENT).
PROJECT_URL = "https://github.com/janisc/hylly"
# How Hylly names itself to the sites it asks: which project this is, and where their people can read about it.
USER_AGENT = f"hylly/{__version__} (personal library; +{PROJECT_URL})"
# How Hylly is installed, like any command-line tool, and installed again over itself for an update: from the project's
# address, with the versions of its libraries it was tested with (tool-constraints.txt, beside it there) and without what
# tool-overrides.txt leaves out, as uv takes no lock for a tool. The program and the two files from one branch, main,
# named in both. On PYTHON: uv would otherwise take any Python it finds that will do, an older one too, which the doctor
# then calls old; the newest whose fixes updates.PYTHONS knows (a test keeps them the same). The help fills it in, and
# the README gives it as it is.
BRANCH = "main"
PYTHON = "3.14"
_FILES = PROJECT_URL.replace("https://github.com/", "https://raw.githubusercontent.com/") + f"/{BRANCH}"
INSTALL = (f"uv tool install --force --python {PYTHON} git+{PROJECT_URL}@{BRANCH} "
           f"--overrides {_FILES}/tool-overrides.txt --constraints {_FILES}/tool-constraints.txt")
