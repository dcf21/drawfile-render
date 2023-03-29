# -*- coding: utf-8 -*-
# temporary_directory.py
#
# The Python script in this file is part of drawfile-render: a tool for
# rendering Acorn !Draw files to PNG and SVG.
#
# Copyright (C) 2010-2023 Dominic Ford <https://dcford.org.uk/>
#
# This code is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# You should have received a copy of the GNU General Public License along with
# this file; if not, write to the Free Software Foundation, Inc., 51 Franklin
# Street, Fifth Floor, Boston, MA  02110-1301, USA

# ----------------------------------------------------------------------------

"""
Class to create a temporary working directory, and clean up its contents afterwards
"""

import hashlib
import os
import shutil
import time


class TemporaryDirectory:
    """
    Class to create a temporary working directory, and clean up its contents afterwards
    """

    def __init__(self):
        # Create a random hex id to use in the filename of the temporary directory
        key_string = str(time.time())
        uid = hashlib.md5(key_string.encode()).hexdigest()

        # Create temporary working directory
        identifier = uid[:32]
        id_string = "computer_archive_{:d}_{}".format(os.getpid(), identifier)
        tmp_dir = os.path.join("/tmp", id_string)
        os.makedirs(name=tmp_dir, mode=0o700, exist_ok=True)

        self.tmp_dir = tmp_dir

    def __enter__(self):
        """
        Called at the start of a with block
        """
        return self

    def __del__(self):
        """
        Destructor
        """
        self.clean_up()

    def clean_up(self):
        """
        Clean up temporary directory
        """
        # Iteratively delete temporary directory and all its contents
        if self.tmp_dir is not None:
            shutil.rmtree(self.tmp_dir)
            self.tmp_dir = None

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Called at the end of a with block
        """
        self.clean_up()
